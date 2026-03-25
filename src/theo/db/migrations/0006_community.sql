-- Graph communities: hierarchical clusters for large-scale retrieval.
--
-- As the knowledge graph grows to millions of nodes over years of use,
-- pre-computed community structure enables hierarchical summarization
-- and efficient scoped retrieval (narrow search space before vector search).
-- Designed for batch community detection (Leiden algorithm).

-- Communities ----------------------------------------------------------------

CREATE TABLE IF NOT EXISTS community (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    level integer NOT NULL DEFAULT 0,
    summary text,
    embedding vector(768),
    node_count integer NOT NULL DEFAULT 0,
    parent_id bigint REFERENCES community (id) ON DELETE SET NULL,
    meta jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT community_no_self_parent
    CHECK (parent_id IS DISTINCT FROM id)
);

CREATE OR REPLACE TRIGGER trg_community_updated_at
BEFORE UPDATE ON community
FOR EACH ROW
EXECUTE FUNCTION _set_updated_at();

-- Hierarchical retrieval: communities at a given level.
CREATE INDEX IF NOT EXISTS ix_community_level
ON community (level, created_at DESC);

-- Rows awaiting async embedding computation.
CREATE INDEX IF NOT EXISTS ix_community_needs_embedding
ON community (id) WHERE embedding IS NULL;

-- Children of a parent community.
CREATE INDEX IF NOT EXISTS ix_community_parent
ON community (parent_id) WHERE parent_id IS NOT NULL;

-- Community-node assignments ------------------------------------------------

CREATE TABLE IF NOT EXISTS community_node (
    community_id bigint NOT NULL REFERENCES community (id) ON DELETE CASCADE,
    node_id bigint NOT NULL REFERENCES node (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (community_id, node_id)
);

-- Which communities does this node belong to?
CREATE INDEX IF NOT EXISTS ix_community_node_node
ON community_node (node_id);
