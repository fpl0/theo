-- Episodic memory: append-only event stream with consolidation support.
--
-- Separate from the knowledge graph because episodic data has fundamentally
-- different access patterns: append-only, time-ordered, session-scoped,
-- high-volume.

CREATE TABLE IF NOT EXISTS episode (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id uuid NOT NULL,
    channel text NOT NULL DEFAULT 'internal'
    CONSTRAINT episode_channel_check
    CHECK (channel IN (
        'message', 'email', 'web', 'observe', 'cli', 'internal'
    )),
    role text NOT NULL
    CONSTRAINT episode_role_check
    CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    body text NOT NULL,
    embedding vector(768),
    trust trust_tier NOT NULL DEFAULT 'owner',
    importance real NOT NULL DEFAULT 0.5
    CONSTRAINT episode_importance_range
    CHECK (importance >= 0.0 AND importance <= 1.0),
    sensitivity sensitivity_level NOT NULL DEFAULT 'normal',
    access_count integer NOT NULL DEFAULT 0,
    last_accessed_at timestamptz,
    meta jsonb NOT NULL DEFAULT '{}',
    superseded_by bigint REFERENCES episode (id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT episode_no_self_supersede
    CHECK (superseded_by IS DISTINCT FROM id)
);

-- HOT update headroom + aggressive autovacuum for frequent access_count writes.
ALTER TABLE episode SET (
    fillfactor = 85,
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);

-- Time-ordered retrieval within a session.
CREATE INDEX IF NOT EXISTS ix_episode_session
ON episode (session_id, created_at);

-- Active (non-superseded) episodes only — the default read path.
CREATE INDEX IF NOT EXISTS ix_episode_active
ON episode (session_id, created_at) WHERE superseded_by IS NULL;

-- Semantic recall across sessions via HNSW.
CREATE INDEX IF NOT EXISTS ix_episode_embedding
ON episode USING hnsw (embedding vector_cosine_ops)
WITH (m = 24, ef_construction = 128);

-- Multi-signal retrieval: importance + recency.
CREATE INDEX IF NOT EXISTS ix_episode_importance
ON episode (importance DESC, created_at DESC);

-- FK index on superseded_by: prevents sequential scan on cascade delete.
CREATE INDEX IF NOT EXISTS ix_episode_superseded_by
ON episode (superseded_by) WHERE superseded_by IS NOT NULL;

-- Cross-session channel queries ("show me all messages this week").
CREATE INDEX IF NOT EXISTS ix_episode_channel
ON episode (channel, created_at DESC);

-- Rows awaiting async embedding computation.
CREATE INDEX IF NOT EXISTS ix_episode_needs_embedding
ON episode (id) WHERE embedding IS NULL;

-- Cross-tier: track which node was promoted from an episode.
ALTER TABLE node
ADD COLUMN source_episode_id bigint;

ALTER TABLE node
ADD CONSTRAINT fk_node_source_episode
FOREIGN KEY (source_episode_id) REFERENCES episode (id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_node_source_episode
ON node (source_episode_id) WHERE source_episode_id IS NOT NULL;

-- Cross-tier references: link episodes to the entities they mention.
-- Enables "tell me everything about X" across both tiers.
CREATE TABLE IF NOT EXISTS episode_node (
    episode_id bigint NOT NULL REFERENCES episode (id) ON DELETE CASCADE,
    node_id bigint NOT NULL REFERENCES node (id) ON DELETE CASCADE,
    role text NOT NULL DEFAULT 'mention'
    CONSTRAINT episode_node_role_check
    CHECK (role IN ('mention', 'subject', 'derived_from')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (episode_id, node_id, role)
);

-- Which episodes mention this entity?
CREATE INDEX IF NOT EXISTS ix_episode_node_node
ON episode_node (node_id, created_at DESC);

-- Consolidation provenance: which episodes does a summary compress?
CREATE TABLE IF NOT EXISTS episode_summary (
    summary_id bigint NOT NULL REFERENCES episode (id) ON DELETE CASCADE,
    source_id bigint NOT NULL REFERENCES episode (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (summary_id, source_id)
);

CREATE INDEX IF NOT EXISTS ix_episode_summary_source
ON episode_summary (source_id);
