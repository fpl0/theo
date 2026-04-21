-- ============================================================================
-- Migration 0006: Memory resilience (Phase 13a)
--
-- Structural hardening for long-horizon operation:
--   * episode.importance -- salience score used as a consolidation gate
--   * node.metadata      -- optional structured attributes per kind
--   * node.source_event_id -- provenance back to the creating event
--   * self_model_domain.recent_* -- windowed calibration (30 days / 50 preds)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- episode.importance: salience score used by the consolidation gate.
-- Default 0.5 is neutral. Episodes with importance >= 0.8 are preserved at
-- full fidelity (never compressed by the background consolidator).
-- ---------------------------------------------------------------------------

ALTER TABLE episode ADD COLUMN IF NOT EXISTS importance real NOT NULL DEFAULT 0.5
  CHECK (importance >= 0.0 AND importance <= 1.0);

-- Partial index: consolidation needs to prioritize by importance among
-- the non-superseded candidates. The DESC order mirrors consolidation's
-- natural read pattern (highest-salience first when triaging).
CREATE INDEX IF NOT EXISTS idx_episode_importance ON episode (importance DESC)
  WHERE superseded_by IS NULL;

-- ---------------------------------------------------------------------------
-- node.metadata: open structured attributes per kind.
-- Advisory only -- body stays the embeddable/searchable text. No GIN index
-- yet; add one when the first structured query pattern surfaces.
-- ---------------------------------------------------------------------------

ALTER TABLE node ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}';

-- ---------------------------------------------------------------------------
-- node.source_event_id: ULID of the memory.node.created event.
-- Nullable because existing nodes predate the column, and because future
-- ingestion paths (e.g., replay) may still skip it.
-- ---------------------------------------------------------------------------

ALTER TABLE node ADD COLUMN IF NOT EXISTS source_event_id text;

CREATE INDEX IF NOT EXISTS idx_node_source_event ON node (source_event_id)
  WHERE source_event_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- self_model_domain.recent_*: windowed calibration counters.
-- The window resets every 30 days or every 50 predictions, whichever comes
-- first (enforced in application code via recordPrediction/recordOutcome).
-- ---------------------------------------------------------------------------

ALTER TABLE self_model_domain
  ADD COLUMN IF NOT EXISTS recent_predictions integer NOT NULL DEFAULT 0
    CHECK (recent_predictions >= 0),
  ADD COLUMN IF NOT EXISTS recent_correct integer NOT NULL DEFAULT 0
    CHECK (recent_correct >= 0 AND recent_correct <= recent_predictions),
  ADD COLUMN IF NOT EXISTS window_reset_at timestamptz NOT NULL DEFAULT now();
