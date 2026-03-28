-- Intent queue: persistent priority queue for proactive actions.
-- Intents represent things Theo wants to do but hasn't yet.
-- Separate from the event bus (which handles immutable facts).

CREATE TABLE IF NOT EXISTS intent (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    type text NOT NULL,
    state text NOT NULL DEFAULT 'proposed'
    CONSTRAINT intent_state_check
    CHECK (
        state IN (
            'proposed', 'approved', 'executing',
            'completed', 'failed', 'expired', 'cancelled'
        )
    ),
    base_priority int NOT NULL DEFAULT 50,
    source_module text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}',
    deadline timestamptz,
    budget_tokens int,
    attempts int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 3,
    result jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz,
    started_at timestamptz,
    completed_at timestamptz,
    expires_at timestamptz,
    CONSTRAINT intent_priority_range
    CHECK (base_priority >= 0 AND base_priority <= 100),
    CONSTRAINT intent_attempts_range
    CHECK (attempts >= 0 AND attempts <= max_attempts),
    CONSTRAINT intent_max_attempts_positive
    CHECK (max_attempts >= 1)
);

-- Evaluator hot path: fetch highest-priority actionable intents.
CREATE INDEX IF NOT EXISTS ix_intent_evaluator
ON intent (base_priority DESC, created_at ASC)
WHERE state IN ('proposed', 'approved');

-- Find active (executing) intents for throttle decisions.
CREATE INDEX IF NOT EXISTS ix_intent_active
ON intent (started_at DESC)
WHERE state = 'executing';

-- Expired intent cleanup.
CREATE INDEX IF NOT EXISTS ix_intent_expires
ON intent (expires_at ASC)
WHERE expires_at IS NOT NULL AND state IN ('proposed', 'approved');

-- Auto-update updated_at on row modification.
CREATE TRIGGER intent_set_updated_at
BEFORE UPDATE ON intent
FOR EACH ROW EXECUTE FUNCTION _set_updated_at();
