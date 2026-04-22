-- ============================================================================
-- Migration 0008: Autonomous Ideation & Reflexes (Phase 13b)
--
-- Adds the tables and columns needed for the webhook gate, reflex handling,
-- ideation job, proposal lifecycle, consent ledger, egress filter, trust-
-- propagation storage, and the degradation ladder.
--
-- Every new table is independently projected from `*.*` events; the only
-- table that stores information not derivable from the log is
-- `webhook_secret` (key material — never in events by design) and
-- `webhook_body` (transient, 24 h TTL for content replay).
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Webhook delivery dedup.
--
-- At-least-once delivery is expected from GitHub/Linear/email relays. A
-- unique (source, delivery_id) insert is the dedup race winner; duplicates
-- return 200 without processing.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS webhook_delivery (
  source        text        NOT NULL,
  delivery_id   text        NOT NULL,
  received_at   timestamptz NOT NULL DEFAULT now(),
  signature_ok  boolean     NOT NULL,
  outcome       text        NOT NULL
                CHECK (outcome IN ('accepted','rejected','rate_limited','stale','duplicate')),
  PRIMARY KEY (source, delivery_id)
);

CREATE INDEX IF NOT EXISTS idx_webhook_delivery_received ON webhook_delivery (received_at);

-- ---------------------------------------------------------------------------
-- Webhook secrets — rotatable, never in event log.
--
-- `secret_previous` holds the previous key during the 7-day rotation grace
-- window. Verification accepts either current or previous; after expiry the
-- previous column is cleared and `webhook.secret_grace_expired` is emitted.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS webhook_secret (
  source                       text        PRIMARY KEY,
  secret_current               text        NOT NULL,
  secret_previous              text,
  secret_previous_expires_at   timestamptz,
  rotated_at                   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Transient webhook body storage.
--
-- The event log captures a body hash and byte length (safe) but not the raw
-- payload. The raw payload lands in this table referenced by
-- `webhook.verified.payloadRef`. Rows expire 24 h after creation so the
-- privacy surface is bounded.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS webhook_body (
  id              text        PRIMARY KEY,  -- ULID
  source          text        NOT NULL,
  body            jsonb       NOT NULL,
  expires_at      timestamptz NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_webhook_body_expires ON webhook_body (expires_at);

-- ---------------------------------------------------------------------------
-- Per-source rate limit — token bucket.
--
-- Default policy seeded on first write (60 req/min, burst 10). Default
-- empty — sources register themselves via the gate's first verified hit.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS reflex_rate_limit (
  source        text        PRIMARY KEY,
  tokens        real        NOT NULL,
  last_refill   timestamptz NOT NULL DEFAULT now(),
  capacity      real        NOT NULL,
  refill_rate   real        NOT NULL    -- tokens per second
);

-- ---------------------------------------------------------------------------
-- Ideation run log.
--
-- Projected from `ideation.scheduled` / `ideation.proposed` /
-- `ideation.budget_exceeded`. Stores the cost rollup for fast budget lookups
-- and the proposal count for UI.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ideation_run (
  run_id            text        PRIMARY KEY,  -- ULID
  started_at        timestamptz NOT NULL DEFAULT now(),
  completed_at      timestamptz,
  cost_usd          numeric(8,4) NOT NULL DEFAULT 0,
  proposal_count    integer     NOT NULL DEFAULT 0,
  status            text        NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','completed','failed','budget_exceeded'))
);
CREATE INDEX IF NOT EXISTS idx_ideation_run_started_at ON ideation_run (started_at);

-- Singleton row — consecutive rejection count + next allowed run.
CREATE TABLE IF NOT EXISTS ideation_backoff (
  id                      text        PRIMARY KEY DEFAULT 'singleton'
                          CHECK (id = 'singleton'),
  consecutive_rejections  integer     NOT NULL DEFAULT 0,
  current_interval_sec    integer     NOT NULL DEFAULT 604800,  -- 1 week
  next_run_at             timestamptz NOT NULL DEFAULT now()
);
INSERT INTO ideation_backoff (id) VALUES ('singleton')
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Proposal staging.
--
-- A proposal is a staged artifact with a pending decision. It carries its
-- autonomy requirements (autonomy_domain + required_level), its origin trust
-- (effective_trust from the causation chain), and a TTL.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS proposal (
  id                  text        PRIMARY KEY,  -- ULID
  origin              text        NOT NULL
                      CHECK (origin IN ('ideation','reflex','owner_request','executive')),
  source_cause_id     text        NOT NULL,
  title               text        NOT NULL,
  summary             text        NOT NULL,
  kind                text        NOT NULL
                      CHECK (kind IN (
                        'new_goal','goal_plan','memory_write','code_change',
                        'message_draft','calendar_hold','workflow_change'
                      )),
  payload             jsonb       NOT NULL,
  effective_trust     text        NOT NULL
                      CHECK (effective_trust IN (
                        'owner','owner_confirmed','verified','inferred','external','untrusted'
                      )),
  autonomy_domain     text        NOT NULL,
  required_level      integer     NOT NULL
                      CHECK (required_level BETWEEN 0 AND 5),
  status              text        NOT NULL DEFAULT 'pending'
                      CHECK (status IN (
                        'pending','approved','rejected','executed','expired'
                      )),
  workspace_branch    text,
  workspace_draft_id  text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  expires_at          timestamptz NOT NULL,
  decided_at          timestamptz,
  decided_by          text,
  redacted            boolean     NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_proposal_status ON proposal (status, created_at DESC)
  WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_proposal_expires ON proposal (expires_at)
  WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- Consent ledger — projected from policy.* events.
--
-- Current state of each consent policy. The key policy for this phase is
-- `autonomous_cloud_egress` — any autonomous (non-interactive) cloud turn is
-- gated on this row being enabled=true.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS consent_ledger (
  policy        text        PRIMARY KEY,
  enabled       boolean     NOT NULL,
  scope         text,
  granted_by    text        NOT NULL,
  granted_at    timestamptz NOT NULL DEFAULT now(),
  reason        text
);

-- ---------------------------------------------------------------------------
-- Per-dimension egress policy.
--
-- `user_model_dimension` already exists from Phase 8. We add
-- `egress_sensitivity` and seed per-dimension defaults. The seed is designed
-- conservatively — only communication_style, energy_patterns, and
-- cognitive_preferences are public; archetypes / shadow / individuation
-- markers are local_only (never sent to the cloud).
-- ---------------------------------------------------------------------------

ALTER TABLE user_model_dimension
  ADD COLUMN IF NOT EXISTS egress_sensitivity text NOT NULL DEFAULT 'private'
    CHECK (egress_sensitivity IN ('public','private','local_only'));

UPDATE user_model_dimension SET egress_sensitivity = 'public'
  WHERE name IN ('communication_style','energy_patterns','cognitive_preferences');
UPDATE user_model_dimension SET egress_sensitivity = 'private'
  WHERE name IN ('values','personality_type','boundaries');
UPDATE user_model_dimension SET egress_sensitivity = 'local_only'
  WHERE name IN ('archetypes','shadow_patterns','individuation_markers');

-- ---------------------------------------------------------------------------
-- Events: effective trust column.
--
-- Every durable event now carries `effective_trust_tier` = min(actor_trust,
-- walk(metadata.causeId) over ancestors). Computed at emission time and
-- stored so descendant walks are O(1) per step.
-- ---------------------------------------------------------------------------

ALTER TABLE events
  ADD COLUMN IF NOT EXISTS effective_trust_tier text
    CHECK (effective_trust_tier IN (
      'owner','owner_confirmed','verified','inferred','external','untrusted'
    ));

UPDATE events SET effective_trust_tier = 'owner' WHERE effective_trust_tier IS NULL;
ALTER TABLE events ALTER COLUMN effective_trust_tier SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_effective_trust ON events (effective_trust_tier);

-- ---------------------------------------------------------------------------
-- Degradation state — singleton row.
--
-- Level 0 = healthy; level 4 = essential-only. The level-changed event keeps
-- the full history; this projection is a fast-path read.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS degradation_state (
  id            text        PRIMARY KEY DEFAULT 'singleton'
                CHECK (id = 'singleton'),
  level         integer     NOT NULL DEFAULT 0
                CHECK (level BETWEEN 0 AND 4),
  reason        text        NOT NULL DEFAULT 'initial',
  changed_at    timestamptz NOT NULL DEFAULT now()
);
INSERT INTO degradation_state (id, level, reason)
VALUES ('singleton', 0, 'initial')
ON CONFLICT (id) DO NOTHING;
