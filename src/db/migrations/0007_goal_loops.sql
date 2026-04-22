-- ============================================================================
-- Migration 0007: Goal loops (Phase 12a)
--
-- Introduces the execution-state projections for the BDI executive loop:
--
--   * goal_state       -- per-goal runtime state (status, priority, lease,
--                         counters). A projection over `goal.*` events.
--   * goal_task        -- per-task runtime state inside a goal's plan.
--   * resume_context   -- opaque partial-turn snapshots for preemption resume.
--   * autonomy_policy  -- per-domain autonomy levels (foundation.md §7.7).
--
-- `goal_state.node_id` FKs to node(id) where `node.kind = 'goal'` — a goal is
-- graph-native (see foundation.md §7.1). The projection holds only
-- fast-changing execution state; the body/title/metadata live on the node.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- goal_state: one row per active goal node.
--
-- `status` is the coarse-grained lifecycle of the goal. `owner_priority` is
-- the user-visible dial (0-100). `effective_trust` is walked from the
-- causation chain at creation time (foundation.md §7.3) and never changes.
-- `plan` is a full snapshot of the most recent plan; `plan_version` is the
-- monotonically-increasing counter carried on `goal.plan_updated`.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS goal_state (
  node_id               integer     PRIMARY KEY REFERENCES node(id) ON DELETE CASCADE,
  status                text        NOT NULL DEFAULT 'proposed'
                        CHECK (status IN (
                          'proposed','active','blocked','paused','completed',
                          'cancelled','quarantined','expired'
                        )),
  origin                text        NOT NULL
                        CHECK (origin IN ('owner','ideation','reflex','system')),
  owner_priority        integer     NOT NULL DEFAULT 50
                        CHECK (owner_priority BETWEEN 0 AND 100),
  effective_trust       text        NOT NULL
                        CHECK (effective_trust IN (
                          'owner','owner_confirmed','verified',
                          'inferred','external','untrusted'
                        )),
  plan_version          integer     NOT NULL DEFAULT 0,
  plan                  jsonb       NOT NULL DEFAULT '[]',
  current_task_id       text,         -- ULID, NULL when no task in flight
  consecutive_failures  integer     NOT NULL DEFAULT 0,
  leased_by             text,         -- runner_id (ULID)
  leased_until          timestamptz,
  blocked_reason        text,
  quarantined_reason    text,
  last_reconsidered_at  timestamptz,
  last_worked_at        timestamptz,
  proposed_expires_at   timestamptz,  -- for ideation proposals
  redacted              boolean     NOT NULL DEFAULT false,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);

-- Hot-path lease acquisition index: ORDER BY priority DESC, then aging term.
-- Partial index keeps the row set small — only active/blocked goals compete
-- for the runner. NULLS FIRST on last_worked_at promotes goals that have never
-- been worked to the front of the queue.
CREATE INDEX IF NOT EXISTS idx_goal_state_status_priority ON goal_state
  (status, owner_priority DESC, last_worked_at NULLS FIRST)
  WHERE status IN ('active','blocked');

-- Lease expiry scan — recovery loop walks leased rows past their `leased_until`.
CREATE INDEX IF NOT EXISTS idx_goal_state_lease ON goal_state (leased_until)
  WHERE leased_by IS NOT NULL;

-- Ideation proposal expiry scan.
CREATE INDEX IF NOT EXISTS idx_goal_state_proposed_expires ON goal_state
  (proposed_expires_at) WHERE status = 'proposed';

-- updated_at trigger (function defined in 0003_memory_tables.sql).
CREATE OR REPLACE TRIGGER trg_goal_state_updated_at
  BEFORE UPDATE ON goal_state
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ---------------------------------------------------------------------------
-- goal_task: per-task execution state inside a plan.
--
-- `task_id` is a ULID minted when the planner produces a `goal.plan_updated`
-- event. `plan_version` is the version number of the plan the task belongs to
-- — when a new plan lands, prior-version tasks are marked `abandoned` by the
-- projection (no separate event needed).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS goal_task (
  task_id         text        PRIMARY KEY,  -- ULID
  goal_node_id    integer     NOT NULL REFERENCES goal_state(node_id) ON DELETE CASCADE,
  plan_version    integer     NOT NULL,
  step_order      integer     NOT NULL,     -- position inside the plan snapshot
  body            text        NOT NULL,
  depends_on      text[]      NOT NULL DEFAULT '{}',  -- other task_ids
  status          text        NOT NULL DEFAULT 'pending'
                  CHECK (status IN (
                    'pending','in_progress','yielded','completed',
                    'failed','abandoned'
                  )),
  subagent        text,                          -- preferred subagent, if chosen
  started_at      timestamptz,
  completed_at    timestamptz,
  last_turn_id    text,                          -- ULID of most recent `goal.task_started`
  last_runner_id  text,                          -- ULID of executive process instance
  failure_count   integer     NOT NULL DEFAULT 0,
  yield_count     integer     NOT NULL DEFAULT 0,
  resume_key      text,                          -- points at resume_context.id
  redacted        boolean     NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now()
);

-- Ordered plan traversal: (goal_node_id, plan_version, step_order).
CREATE INDEX IF NOT EXISTS idx_goal_task_goal ON goal_task
  (goal_node_id, plan_version, step_order);

-- Active task lookup by goal — partial index on live statuses.
CREATE INDEX IF NOT EXISTS idx_goal_task_status ON goal_task (status, goal_node_id)
  WHERE status IN ('in_progress','yielded','pending');

-- ---------------------------------------------------------------------------
-- resume_context: opaque partial-turn state for preemption resume.
--
-- Points-at, not replayable. When an executive turn is preempted with
-- `goal.task_yielded(reason=preempted)`, the subagent emits a snapshot and the
-- row's id is stored on the event. A later turn can re-hydrate from the row.
-- Rows expire after 24h; a GC job sweeps them.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS resume_context (
  id              text        PRIMARY KEY,  -- ULID
  goal_node_id    integer     NOT NULL REFERENCES goal_state(node_id) ON DELETE CASCADE,
  task_id         text        NOT NULL,
  turn_id         text        NOT NULL,
  session_id      text,
  snapshot        jsonb       NOT NULL,     -- opaque, whatever the subagent produced
  token_count     integer     NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz NOT NULL      -- 24 h from creation
);

CREATE INDEX IF NOT EXISTS idx_resume_context_goal ON resume_context (goal_node_id, task_id);
CREATE INDEX IF NOT EXISTS idx_resume_context_expires ON resume_context (expires_at);

-- ---------------------------------------------------------------------------
-- autonomy_policy: per-domain autonomy levels (foundation.md §7.7).
--
-- Levels 0-5:
--   0 = suspend  — domain frozen, no autonomous action
--   1 = ask      — propose, require owner confirmation
--   2 = propose  — propose, act on owner approval (ideation cap)
--   3 = ack      — act, acknowledge afterwards
--   4 = summary  — act, summarize periodically
--   5 = silent   — act without notification (never for restricted domains)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS autonomy_policy (
  domain          text        PRIMARY KEY,
  level           integer     NOT NULL
                  CHECK (level BETWEEN 0 AND 5),
  set_by          text        NOT NULL,     -- actor kind
  set_at          timestamptz NOT NULL DEFAULT now(),
  reason          text
);

-- Seed default autonomy levels. Conservative defaults — anything irreversible
-- or reaching outside the workspace is 0-1 until the owner explicitly raises.
INSERT INTO autonomy_policy (domain, level, set_by) VALUES
  ('code.read',                    5, 'system'),
  ('code.write.workspace',         3, 'system'),
  ('code.write.theo_source',       0, 'system'),
  ('code.push.remote',             0, 'system'),
  ('messaging.draft',              3, 'system'),
  ('messaging.send',               0, 'system'),
  ('calendar.read',                5, 'system'),
  ('calendar.write',               4, 'system'),
  ('financial.read',               0, 'system'),
  ('financial.write',              0, 'system'),
  ('memory.write.ideation_origin', 2, 'system'),
  ('system.config',                0, 'system'),
  ('workspace.cleanup',            5, 'system')
ON CONFLICT (domain) DO NOTHING;
