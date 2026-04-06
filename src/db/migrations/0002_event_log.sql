-- Partitioned events table
CREATE TABLE IF NOT EXISTS events (
  id         text        NOT NULL,  -- ULID
  type       text        NOT NULL,
  version    integer     NOT NULL DEFAULT 1,
  timestamp  timestamptz NOT NULL DEFAULT now(),
  actor      text        NOT NULL,
  data       jsonb       NOT NULL DEFAULT '{}',
  metadata   jsonb       NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id, timestamp)     -- must include partition key
) PARTITION BY RANGE (timestamp);

-- Composite index for type-filtered reads: WHERE type = ANY(...) ORDER BY id
-- Inherited by each partition automatically.
CREATE INDEX IF NOT EXISTS idx_events_type_id ON events (type, id);

-- Handler checkpoint cursors
CREATE TABLE IF NOT EXISTS handler_cursors (
  handler_id text        PRIMARY KEY,
  cursor     text        NOT NULL,  -- ULID of last processed event
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Projection snapshots (for future Phase 13 consolidation)
CREATE TABLE IF NOT EXISTS event_snapshots (
  projection text        PRIMARY KEY,
  state      jsonb       NOT NULL,
  cursor     text        NOT NULL,  -- ULID of last event included
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
