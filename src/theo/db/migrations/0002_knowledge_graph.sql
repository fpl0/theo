-- Knowledge graph: nodes and edges for Theo's semantic memory.
--
-- Nodes are anything Theo remembers. Edges are labeled, weighted,
-- temporally-valid relationships between nodes.

-- Nodes ----------------------------------------------------------------

CREATE TABLE IF NOT EXISTS node (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind text NOT NULL,
    body text NOT NULL,
    embedding vector(768),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED,
    trust trust_tier NOT NULL DEFAULT 'inferred',
    confidence real NOT NULL DEFAULT 0.5
    CONSTRAINT node_confidence_range
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    evidence_count integer NOT NULL DEFAULT 1
    CONSTRAINT node_evidence_count_positive
    CHECK (evidence_count >= 1),
    importance real NOT NULL DEFAULT 0.5
    CONSTRAINT node_importance_range
    CHECK (importance >= 0.0 AND importance <= 1.0),
    sensitivity sensitivity_level NOT NULL DEFAULT 'normal',
    access_count integer NOT NULL DEFAULT 0,
    last_accessed_at timestamptz,
    meta jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- HOT update headroom + aggressive autovacuum for frequent access_count writes.
ALTER TABLE node SET (
    fillfactor = 85,
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);

CREATE OR REPLACE TRIGGER trg_node_updated_at
BEFORE UPDATE ON node
FOR EACH ROW
EXECUTE FUNCTION _set_updated_at();

-- HNSW for vector similarity (m=24 for better recall at 768-dim).
CREATE INDEX IF NOT EXISTS ix_node_embedding
ON node USING hnsw (embedding vector_cosine_ops)
WITH (m = 24, ef_construction = 128);

-- GIN for full-text search.
CREATE INDEX IF NOT EXISTS ix_node_tsv
ON node USING gin (tsv);

-- Kind + time for filtered, time-ordered retrieval.
CREATE INDEX IF NOT EXISTS ix_node_kind
ON node (kind, created_at);

-- Owner-tier nodes: the only trust-filtered query needing speed.
CREATE INDEX IF NOT EXISTS ix_node_owner
ON node (kind, created_at DESC) WHERE trust = 'owner';

-- Multi-signal retrieval: importance ranking per kind.
-- last_accessed_at intentionally excluded to keep access_count/last_accessed_at
-- updates HOT-eligible (heap-only tuple — no index maintenance).
CREATE INDEX IF NOT EXISTS ix_node_importance
ON node (kind, importance DESC);

-- Rows awaiting async embedding computation.
CREATE INDEX IF NOT EXISTS ix_node_needs_embedding
ON node (id) WHERE embedding IS NULL;

-- Edges ----------------------------------------------------------------

CREATE TABLE IF NOT EXISTS edge (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id bigint NOT NULL REFERENCES node (id) ON DELETE CASCADE,
    target_id bigint NOT NULL REFERENCES node (id) ON DELETE CASCADE,
    label text NOT NULL,
    weight real NOT NULL DEFAULT 1.0
    CONSTRAINT edge_weight_range
    CHECK (weight >= 0.0 AND weight <= 1.0),
    meta jsonb NOT NULL DEFAULT '{}',
    valid_from timestamptz NOT NULL DEFAULT now(),
    valid_to timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT edge_no_self_loop CHECK (source_id <> target_id),
    CONSTRAINT edge_valid_range
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

-- Graph traversal: edges of type X from/to node Y.
CREATE INDEX IF NOT EXISTS ix_edge_source_label ON edge (source_id, label);
CREATE INDEX IF NOT EXISTS ix_edge_target_label ON edge (target_id, label);

-- Only one active edge of a given type between two nodes.
CREATE UNIQUE INDEX IF NOT EXISTS ix_edge_active_unique
ON edge (source_id, target_id, label) WHERE valid_to IS NULL;
