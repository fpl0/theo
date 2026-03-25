-- Cross-tier references: link episodes to the entities they mention.
--
-- Without this, the tiers are siloed — "tell me everything about X"
-- can search nodes OR episodes but cannot traverse between them.
-- CoMem and A-MEM both demonstrate that linking episodic memories
-- to extracted entities enables richer, more complete retrieval.
--
-- This is a many-to-many junction: one episode may mention several
-- entities, and one entity may appear across many episodes.

CREATE TABLE IF NOT EXISTS episode_node (
    episode_id  bigint NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    node_id     bigint NOT NULL REFERENCES node(id)    ON DELETE CASCADE,
    role        text   NOT NULL DEFAULT 'mention'
                    CONSTRAINT episode_node_role_check
                        CHECK (role IN ('mention', 'subject', 'derived_from')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (episode_id, node_id, role)
);

-- "Which episodes mention this entity?" — the primary cross-tier query.
CREATE INDEX IF NOT EXISTS ix_episode_node_node
    ON episode_node (node_id, created_at DESC);
