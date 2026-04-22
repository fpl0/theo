-- ============================================================================
-- Migration 0009: Phase 15 Operationalization
--
-- Two small additions:
--
--   1. `proposal.payload_hash` — SHA-256 of the payload at request time.
--      `approveProposal` verifies the stored hash matches a caller-supplied
--      expected hash, closing a TOCTOU vector where a proposal's payload
--      could be mutated between presentation and approval. Computed at the
--      application layer (Bun SubtleCrypto) — no pgcrypto dependency.
--
--      Pre-production: the foundation plan has no persisted proposals. The
--      column is created with an empty-string default, the default is then
--      dropped, and application code writes the real hash on every insert.
--
--   2. `self_update_state` — singleton table tracking `healthy_commit` and
--      the last rollback. Mirrors what the filesystem stores under
--      `~/Theo/data/healthy_commit`, but in the DB so `launchd`-less
--      environments (CI, tests) have a single source of truth too.
-- ============================================================================

ALTER TABLE IF EXISTS proposal
  ADD COLUMN IF NOT EXISTS payload_hash text NOT NULL DEFAULT '';

ALTER TABLE IF EXISTS proposal
  ALTER COLUMN payload_hash DROP DEFAULT;

-- ---------------------------------------------------------------------------
-- Self-update state — singleton row.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS self_update_state (
  id               text        PRIMARY KEY DEFAULT 'singleton',
  healthy_commit   text,
  last_rollback_at timestamptz,
  last_rollback_to text,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT self_update_state_singleton CHECK (id = 'singleton')
);

INSERT INTO self_update_state (id) VALUES ('singleton')
ON CONFLICT (id) DO NOTHING;
