-- Deliberation: persistent state for multi-step reasoning sessions.
--
-- Stores the full lifecycle of a deliberation — from framing the question
-- through gathering evidence, generating candidates, evaluating them, and
-- synthesizing a final answer.  Each row holds the complete state so
-- recovery and context assembly require only a single-row read.

CREATE TABLE IF NOT EXISTS deliberation (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    deliberation_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    session_id uuid NOT NULL,
    question text NOT NULL,
    phase text NOT NULL DEFAULT 'frame'
    CONSTRAINT deliberation_phase_valid
    CHECK (
        phase IN (
            'frame',
            'gather',
            'generate',
            'evaluate',
            'synthesize',
            'complete'
        )
    ),
    phase_outputs jsonb NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'running'
    CONSTRAINT deliberation_status_valid
    CHECK (
        status IN ('running', 'completed', 'failed', 'cancelled')
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    delivered boolean NOT NULL DEFAULT FALSE
);

CREATE OR REPLACE TRIGGER trg_deliberation_updated_at
BEFORE UPDATE ON deliberation
FOR EACH ROW
EXECUTE FUNCTION _set_updated_at();

-- Evaluator finds active deliberations for a session.
CREATE INDEX IF NOT EXISTS ix_deliberation_active
ON deliberation (session_id, created_at)
WHERE status = 'running';

-- Delivery finds completed but undelivered results.
CREATE INDEX IF NOT EXISTS ix_deliberation_pending_delivery
ON deliberation (created_at)
WHERE status = 'completed' AND NOT delivered;
