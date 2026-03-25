-- Consolidation: episode summarization chains and memory promotion.
--
-- AgeMem's "summarise" operation compresses old episodes into
-- higher-level summaries.  MemoryBank's forgetting curve deprioritises
-- stale, unaccessed memories.  Both require schema support.
--
-- Design:
--   - A summary is itself an episode (same table, queryable the same way).
--   - summary_of links a summary episode back to the episodes it replaced.
--   - Summarised episodes are marked superseded (not deleted) so the
--     full history remains available for audit or deep retrieval.
--   - Nodes track their origin: was this fact extracted from an episode
--     during consolidation, or created directly by the agent?

-- Mark episodes that have been consolidated into a summary.
-- NULL = active, non-NULL = superseded by that summary episode.
ALTER TABLE episode
    ADD COLUMN superseded_by bigint REFERENCES episode(id);

-- Summary provenance: which episodes does a summary compress?
CREATE TABLE IF NOT EXISTS episode_summary (
    summary_id  bigint NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    source_id   bigint NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (summary_id, source_id)
);

-- "What was this summary made from?" and "Is this episode summarised?"
CREATE INDEX IF NOT EXISTS ix_episode_summary_source
    ON episode_summary (source_id);

-- Only retrieve active (non-superseded) episodes by default.
CREATE INDEX IF NOT EXISTS ix_episode_active
    ON episode (session_id, created_at) WHERE superseded_by IS NULL;

-- Node provenance: track which episode a node was extracted from
-- during consolidation (promotion from episodic → semantic tier).
-- NULL = created directly by agent or user, not promoted.
ALTER TABLE node
    ADD COLUMN source_episode_id bigint REFERENCES episode(id) ON DELETE SET NULL;

-- Reflection support: a node with kind = 'reflection' is a
-- higher-order insight derived from reasoning over other memories.
-- source_node_ids in meta tracks which nodes were reflected upon.
-- No new table needed — reflections are nodes with a distinguished kind,
-- but we add a partial index for efficient reflection retrieval.
CREATE INDEX IF NOT EXISTS ix_node_reflections
    ON node (created_at DESC) WHERE kind = 'reflection';
