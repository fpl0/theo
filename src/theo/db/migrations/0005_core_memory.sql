-- Core memory: small JSONB documents always included in the context window.
--
-- The agent's "RAM" — persona, goals, active user model, current task
-- context.  Sub-10 KB total.  Read every turn, written rarely.
-- Maps to MemGPT core tier / five-type working memory + user model.

CREATE TABLE IF NOT EXISTS core_memory (
    label       text        PRIMARY KEY,
    body        jsonb       NOT NULL DEFAULT '{}',
    version     integer     NOT NULL DEFAULT 1,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE TRIGGER trg_core_memory_updated_at
    BEFORE UPDATE ON core_memory
    FOR EACH ROW
    EXECUTE FUNCTION _set_updated_at();

-- Seed canonical slots so the agent always has a document to read.
INSERT INTO core_memory (label, body) VALUES
    ('persona',    '{"summary": "Theo is a personal AI agent."}'),
    ('goals',      '{"active": [], "completed": []}'),
    ('user_model', '{"preferences": {}, "values": {}, "traits": {}}'),
    ('context',    '{"current_task": null, "focus": null}')
ON CONFLICT (label) DO NOTHING;
