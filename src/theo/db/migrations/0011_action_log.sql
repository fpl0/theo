-- Action log: records every autonomy-classified action for audit,
-- outcome tracking, and future autonomy graduation (M5).
--
-- Every action Theo takes — whether autonomous, informed, proposed,
-- or consulted — gets a row here.  The intent_id column links to
-- the intent queue when actions originate from background intents.

CREATE TABLE IF NOT EXISTS action_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action_type text NOT NULL,
    autonomy_level text NOT NULL
    CONSTRAINT action_log_autonomy_level_valid
    CHECK (
        autonomy_level IN (
            'autonomous',
            'inform',
            'propose',
            'consult'
        )
    ),
    decision text NOT NULL
    CONSTRAINT action_log_decision_valid
    CHECK (
        decision IN (
            'executed',
            'approved',
            'rejected',
            'modified',
            'timed_out'
        )
    ),
    context jsonb NOT NULL DEFAULT '{}',
    session_id uuid,
    intent_id bigint,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Query by session for turn-level audit.
CREATE INDEX IF NOT EXISTS ix_action_log_session
ON action_log (session_id, created_at)
WHERE session_id IS NOT NULL;

-- Query by action type for autonomy graduation (M5).
CREATE INDEX IF NOT EXISTS ix_action_log_type
ON action_log (action_type, created_at);

-- Query by intent for linking actions to background intents.
CREATE INDEX IF NOT EXISTS ix_action_log_intent
ON action_log (intent_id)
WHERE intent_id IS NOT NULL;
