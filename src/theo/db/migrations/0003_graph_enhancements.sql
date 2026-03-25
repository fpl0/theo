-- Enhance the knowledge graph with trust provenance, retrieval metadata,
-- confidence semantics, and tuned indexes.

-- Trust provenance: every node carries a trust tier so external data
-- cannot silently override owner-provided facts.
--   owner    – user stated directly (highest)
--   verified – user confirmed external data or agent inference
--   inferred – agent derived from behaviour or reasoning
--   external – sourced from APIs, documents, web (lowest)
ALTER TABLE node
    ADD COLUMN trust       text        NOT NULL DEFAULT 'inferred'
        CONSTRAINT node_trust_check CHECK (trust IN ('owner', 'verified', 'inferred', 'external')),
    ADD COLUMN importance  real        NOT NULL DEFAULT 0.5
        CONSTRAINT node_importance_range CHECK (importance >= 0.0 AND importance <= 1.0),
    ADD COLUMN access_count    integer     NOT NULL DEFAULT 0,
    ADD COLUMN last_accessed_at timestamptz,
    ADD COLUMN source_episode_id bigint;
-- FK added after episode table exists (0004).

-- Confidence semantics: edge weight is a 0–1 confidence score.
ALTER TABLE edge
    ADD CONSTRAINT edge_weight_range CHECK (weight >= 0.0 AND weight <= 1.0);

-- Only one active edge of a given type between two nodes.
-- The agent expires the old edge (sets valid_to) before creating a new one.
CREATE UNIQUE INDEX IF NOT EXISTS ix_edge_active_unique
    ON edge (source_id, target_id, label) WHERE valid_to IS NULL;

-- Partial index for owner-tier nodes (the only trust-filtered query
-- that needs speed; trust has only 4 values so a full composite wastes writes).
CREATE INDEX IF NOT EXISTS ix_node_owner
    ON node (kind, created_at DESC) WHERE trust = 'owner';

-- Multi-signal retrieval: importance + recency (Generative Agents pattern).
CREATE INDEX IF NOT EXISTS ix_node_importance
    ON node (kind, importance DESC, last_accessed_at DESC NULLS LAST);

-- Reflection retrieval: higher-order insights stored as kind='reflection'.
CREATE INDEX IF NOT EXISTS ix_node_reflections
    ON node (created_at DESC) WHERE kind = 'reflection';

-- Rebuild HNSW with tuned params: m=24 gives better recall than default 16
-- for 768-dim vectors; ef_construction=128 is cheap at sub-million scale.
DROP INDEX IF EXISTS ix_node_embedding;
CREATE INDEX ix_node_embedding
    ON node USING hnsw (embedding vector_cosine_ops)
    WITH (m = 24, ef_construction = 128);

-- Track rows awaiting async embedding computation.
CREATE INDEX IF NOT EXISTS ix_node_needs_embedding
    ON node (id) WHERE embedding IS NULL;

-- Fillfactor 85%: access_count/last_accessed_at updates are not in any
-- index, so they qualify for HOT (Heap-Only Tuple) updates — but only
-- if pages have free space.
ALTER TABLE node SET (fillfactor = 85);
