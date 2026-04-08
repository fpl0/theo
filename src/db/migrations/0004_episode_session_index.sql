-- Composite partial index for getBySession query: covers the equality filter
-- (session_id), partial condition (superseded_by IS NULL), and sort order
-- (created_at ASC) in a single index scan. The existing idx_episode_session
-- (non-partial, on session_id alone) remains for queries that include
-- superseded episodes (e.g., Phase 13 consolidation).

CREATE INDEX IF NOT EXISTS idx_episode_session_unsuperseded
  ON episode (session_id, created_at ASC)
  WHERE superseded_by IS NULL;
