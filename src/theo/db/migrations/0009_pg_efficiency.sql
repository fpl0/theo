-- PostgreSQL efficiency pass: index tuning, FK indexes, fillfactor, HNSW params.
--
-- Addresses the following issues identified in a schema audit:
--   1. HNSW indexes use untuned defaults (m=16, ef_construction=64)
--   2. ix_node_kind_trust is low-value (trust has 4 values, kind already narrows)
--   3. ix_episode_role_time leads with low-cardinality column (4 values)
--   4. Missing FK index on episode.superseded_by (cascade scan risk)
--   5. Missing FK index on node.source_episode_id (cascade scan risk)
--   6. No fillfactor on update-hot tables (blocks HOT updates)
--   7. Nullable embeddings silently excluded from HNSW search

-- ============================================================
-- 1. Rebuild HNSW indexes with tuned parameters.
--    m=24 gives better recall than default 16 for 768-dim vectors.
--    ef_construction=128 produces a denser graph at build time
--    (cheap for sub-million row counts).
-- ============================================================

DROP INDEX IF EXISTS ix_node_embedding;
CREATE INDEX ix_node_embedding
    ON node USING hnsw (embedding vector_cosine_ops)
    WITH (m = 24, ef_construction = 128);

DROP INDEX IF EXISTS ix_episode_embedding;
CREATE INDEX ix_episode_embedding
    ON episode USING hnsw (embedding vector_cosine_ops)
    WITH (m = 24, ef_construction = 128);

-- ============================================================
-- 2. Replace ix_node_kind_trust with a targeted partial index.
--    The original composite on (kind, trust) adds write cost on
--    every INSERT/UPDATE for marginal benefit — trust has only
--    4 values.  A partial index on owner-tier nodes is the only
--    trust-filtered query that truly needs speed.
-- ============================================================

DROP INDEX IF EXISTS ix_node_kind_trust;
CREATE INDEX IF NOT EXISTS ix_node_owner
    ON node (kind, created_at DESC) WHERE trust = 'owner';

-- ============================================================
-- 3. Replace ix_episode_role_time with targeted partial indexes.
--    Leading on role (4 values) gives poor B-tree selectivity.
--    Partial indexes for the actual query patterns are smaller
--    and faster.
-- ============================================================

DROP INDEX IF EXISTS ix_episode_role_time;
CREATE INDEX IF NOT EXISTS ix_episode_tool_recent
    ON episode (created_at DESC) WHERE role = 'tool';
CREATE INDEX IF NOT EXISTS ix_episode_user_recent
    ON episode (created_at DESC) WHERE role = 'user';

-- ============================================================
-- 4–5. Add FK indexes for cascade safety.
--    Without these, deleting an episode requires a sequential
--    scan on both episode (superseded_by) and node (source_episode_id).
-- ============================================================

CREATE INDEX IF NOT EXISTS ix_episode_superseded_by
    ON episode (superseded_by) WHERE superseded_by IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_node_source_episode
    ON node (source_episode_id) WHERE source_episode_id IS NOT NULL;

-- ============================================================
-- 6. Set fillfactor for HOT (Heap-Only Tuple) update headroom.
--    access_count and last_accessed_at are updated on every
--    retrieval but are not in any index — perfect HOT candidates
--    if pages have free space.  85% leaves ~1 KB/page for in-place
--    updates, avoiding index write amplification.
-- ============================================================

ALTER TABLE node SET (fillfactor = 85);
ALTER TABLE episode SET (fillfactor = 85);

-- ============================================================
-- 7. Track rows missing embeddings.
--    Embeddings may be deferred (computed async after insert),
--    so we keep the column nullable.  But rows with NULL
--    embeddings are invisible to HNSW search — these partial
--    indexes let the embedding worker find pending rows cheaply.
-- ============================================================

CREATE INDEX IF NOT EXISTS ix_node_needs_embedding
    ON node (id) WHERE embedding IS NULL;
CREATE INDEX IF NOT EXISTS ix_episode_needs_embedding
    ON episode (id) WHERE embedding IS NULL;
