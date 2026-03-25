CREATE EXTENSION IF NOT EXISTS vector;

-- Trigger function for auto-updating updated_at columns.
CREATE OR REPLACE FUNCTION _set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Knowledge nodes: anything Theo remembers.
CREATE TABLE IF NOT EXISTS node (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind text NOT NULL,
    body text NOT NULL,
    embedding vector(768),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED,
    meta jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE TRIGGER trg_node_updated_at
BEFORE UPDATE ON node
FOR EACH ROW
EXECUTE FUNCTION _set_updated_at();

-- HNSW index for vector similarity search.
CREATE INDEX IF NOT EXISTS ix_node_embedding
ON node USING hnsw (embedding vector_cosine_ops);

-- GIN index for full-text search.
CREATE INDEX IF NOT EXISTS ix_node_tsv
ON node USING gin (tsv);

-- Kind + time for filtered, time-ordered retrieval.
CREATE INDEX IF NOT EXISTS ix_node_kind
ON node (kind, created_at);

-- Edges: labeled, weighted, temporally-valid graph.
CREATE TABLE IF NOT EXISTS edge (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id bigint NOT NULL REFERENCES node (id) ON DELETE CASCADE,
    target_id bigint NOT NULL REFERENCES node (id) ON DELETE CASCADE,
    label text NOT NULL,
    weight real NOT NULL DEFAULT 1.0,
    meta jsonb NOT NULL DEFAULT '{}',
    valid_from timestamptz NOT NULL DEFAULT now(),
    valid_to timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Composite indexes for graph traversal: "edges of type X from node Y".
CREATE INDEX IF NOT EXISTS ix_edge_source_label ON edge (source_id, label);
CREATE INDEX IF NOT EXISTS ix_edge_target_label ON edge (target_id, label);

-- Partial index for current (non-expired) edges only.
CREATE INDEX IF NOT EXISTS ix_edge_current
ON edge (source_id, label) WHERE valid_to IS NULL;
