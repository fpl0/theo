-- Persistent event bus: durable queue for event replay on restart.

CREATE TABLE IF NOT EXISTS event_queue (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type text NOT NULL,
    payload jsonb NOT NULL,
    session_id uuid,
    channel text
    CONSTRAINT event_queue_channel_check
    CHECK (
        channel IS NULL
        OR channel IN (
            'message', 'email', 'web', 'observe', 'cli', 'internal'
        )
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    CONSTRAINT event_queue_processed_order
    CHECK (processed_at IS NULL OR processed_at >= created_at)
);

-- Replay path: unprocessed events in creation order.
CREATE INDEX IF NOT EXISTS ix_event_queue_unprocessed
ON event_queue (created_at ASC) WHERE processed_at IS NULL;

-- Lookup by type for selective replay or diagnostics.
CREATE INDEX IF NOT EXISTS ix_event_queue_type
ON event_queue (type, created_at DESC);

-- Session-scoped event history.
CREATE INDEX IF NOT EXISTS ix_event_queue_session
ON event_queue (session_id, created_at)
WHERE session_id IS NOT NULL;
