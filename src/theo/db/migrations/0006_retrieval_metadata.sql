-- Retrieval metadata: importance scoring and access tracking.
--
-- Generative Agents (Park et al. 2023) retrieval uses three signals:
--   recency   – already covered by created_at
--   relevance – already covered by embedding similarity
--   importance – missing until now
--
-- Access tracking enables decay: memories that are never retrieved
-- can be deprioritised or garbage-collected during consolidation.

-- Node importance: agent-assigned score at creation/update time.
-- 0.0 = mundane, 1.0 = critical.  Default 0.5 (neutral).
ALTER TABLE node
    ADD COLUMN importance real NOT NULL DEFAULT 0.5
        CONSTRAINT node_importance_range CHECK (importance >= 0.0 AND importance <= 1.0);

-- Access tracking on node: how many times retrieved, and when last.
ALTER TABLE node
    ADD COLUMN access_count integer NOT NULL DEFAULT 0,
    ADD COLUMN last_accessed_at timestamptz;

-- Episode importance: same semantics.  Tool results and system
-- messages default low; user messages and key decisions score higher.
ALTER TABLE episode
    ADD COLUMN importance real NOT NULL DEFAULT 0.5
        CONSTRAINT episode_importance_range CHECK (importance >= 0.0 AND importance <= 1.0);

ALTER TABLE episode
    ADD COLUMN access_count integer NOT NULL DEFAULT 0,
    ADD COLUMN last_accessed_at timestamptz;

-- Composite index for multi-signal retrieval on node:
-- "most important entities of kind X, most recently accessed first".
CREATE INDEX IF NOT EXISTS ix_node_importance
    ON node (kind, importance DESC, last_accessed_at DESC NULLS LAST);

-- Same for episode: "most important episodes, most recent first".
CREATE INDEX IF NOT EXISTS ix_episode_importance
    ON episode (importance DESC, created_at DESC);
