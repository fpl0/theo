-- Core memory (MemGPT core tier): always-in-context structured document.
--
-- Core memory is the agent's "RAM" — a small (<10 KB) JSONB document that
-- is included in every LLM invocation.  It holds persona, goals, the
-- active user model, and current task context.
--
-- Design decisions:
--   - Single row per memory slot (keyed by `label`).
--   - JSONB body for flexible schema evolution.
--   - Version counter for optimistic concurrency control.
--   - updated_at for staleness detection during sleep-time consolidation.
--
-- Maps to:
--   MemGPT core tier    – always in context, sub-10 KB budget
--   Five-type model     – working memory + user model (structured
--                         specialisation of semantic memory)

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

-- Seed the canonical slots so the agent always has a document to read.
INSERT INTO core_memory (label, body) VALUES
    ('persona',   '{"summary": "Theo is a personal AI agent."}'),
    ('goals',     '{"active": [], "completed": []}'),
    ('user_model','{"preferences": {}, "values": {}, "traits": {}}'),
    ('context',   '{"current_task": null, "focus": null}')
ON CONFLICT (label) DO NOTHING;
