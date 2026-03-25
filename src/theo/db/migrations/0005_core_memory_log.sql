-- Core memory changelog: tracks how persona, goals, and user model evolve.
--
-- Core memory changes are rare, high-signal events. This table enables
-- "how has my understanding evolved?" queries without relying on
-- episodic memory (which may not capture all core memory mutations).

CREATE TABLE IF NOT EXISTS core_memory_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    label text NOT NULL REFERENCES core_memory (label) ON DELETE CASCADE,
    old_body jsonb NOT NULL,
    new_body jsonb NOT NULL,
    version integer NOT NULL,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Retrieve change history for a specific slot.
CREATE INDEX IF NOT EXISTS ix_core_memory_log_label
ON core_memory_log (label, created_at DESC);
