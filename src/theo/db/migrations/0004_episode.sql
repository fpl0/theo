-- Episodic memory (recall tier): append-only event stream.
--
-- Episodic memory has fundamentally different access patterns from the
-- knowledge graph: append-only, time-ordered, session-scoped, and
-- high-volume.  Keeping it separate prevents polluting the knowledge
-- graph with low-connectivity, high-cardinality nodes.
--
-- Maps to:
--   MemGPT recall tier  – searched on demand via semantic similarity
--   Five-type model     – episodic memory ("what happened")
--   AgeMem              – tool-based store/retrieve/summarise operations

CREATE TABLE IF NOT EXISTS episode (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id  uuid        NOT NULL,
    role        text        NOT NULL
                    CONSTRAINT episode_role_check
                        CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    body        text        NOT NULL,
    embedding   vector(768),
    meta        jsonb       NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Time-ordered retrieval within a session (the primary access pattern).
CREATE INDEX IF NOT EXISTS ix_episode_session
    ON episode (session_id, created_at);

-- Semantic recall across sessions: "when did we discuss X?"
CREATE INDEX IF NOT EXISTS ix_episode_embedding
    ON episode USING hnsw (embedding vector_cosine_ops);

-- Recent episodes by role (e.g. latest tool results, latest user messages).
CREATE INDEX IF NOT EXISTS ix_episode_role_time
    ON episode (role, created_at DESC);
