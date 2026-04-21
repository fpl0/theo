-- ============================================================================
-- Migration 0005: Scheduler tables
--
-- Adds the scheduled_job and job_execution tables that back Phase 12's
-- scheduler. Jobs are ULID-keyed (like events) so we can reorder/insert
-- deterministically during replay; executions are ULID-keyed for the same
-- reason. The `idx_scheduled_job_next_run` partial index is what the tick
-- loop hits every minute — keep it tight so SELECT WHERE next_run_at < now()
-- stays index-only.
-- ============================================================================

CREATE TABLE IF NOT EXISTS scheduled_job (
  id              text         PRIMARY KEY,   -- ULID
  name            text         NOT NULL UNIQUE,
  cron            text,                       -- null for one-off
  agent           text         NOT NULL DEFAULT 'main',
  prompt          text         NOT NULL,
  enabled         boolean      NOT NULL DEFAULT true,
  max_duration_ms integer      NOT NULL DEFAULT 300000   -- 5 min default
                  CHECK (max_duration_ms > 0),
  max_budget_usd  numeric(6,4) NOT NULL DEFAULT 0.10
                  CHECK (max_budget_usd >= 0),
  last_run_at     timestamptz,
  next_run_at     timestamptz  NOT NULL,
  created_at      timestamptz  NOT NULL DEFAULT now(),
  updated_at      timestamptz  NOT NULL DEFAULT now()
);

-- `update_updated_at` is defined by migration 0003 and reused here.
CREATE OR REPLACE TRIGGER trg_scheduled_job_updated_at
  BEFORE UPDATE ON scheduled_job
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Partial index: tick loop queries only enabled jobs with a due next_run_at.
CREATE INDEX IF NOT EXISTS idx_scheduled_job_next_run
  ON scheduled_job (next_run_at)
  WHERE enabled = true;

CREATE TABLE IF NOT EXISTS job_execution (
  id              text         PRIMARY KEY,   -- ULID
  job_id          text         NOT NULL REFERENCES scheduled_job(id) ON DELETE CASCADE,
  status          text         NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'completed', 'failed')),
  started_at      timestamptz  NOT NULL DEFAULT now(),
  completed_at    timestamptz,
  duration_ms     integer      CHECK (duration_ms IS NULL OR duration_ms >= 0),
  tokens_used     integer      CHECK (tokens_used IS NULL OR tokens_used >= 0),
  cost_usd        numeric(8,4) CHECK (cost_usd IS NULL OR cost_usd >= 0),
  error_message   text,
  result_summary  text,
  created_at      timestamptz  NOT NULL DEFAULT now()
);

-- FK index for cascading deletes and for `history` queries by job.
CREATE INDEX IF NOT EXISTS idx_job_execution_job ON job_execution (job_id);

-- Lookup by ULID order for "most recent executions" queries.
CREATE INDEX IF NOT EXISTS idx_job_execution_started
  ON job_execution (started_at DESC);
