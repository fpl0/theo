CREATE TABLE IF NOT EXISTS token_usage (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id uuid NOT NULL,
    model text NOT NULL,
    input_tokens int NOT NULL,
    output_tokens int NOT NULL,
    estimated_cost real NOT NULL,
    source text NOT NULL DEFAULT 'conversation',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_token_usage_session_created
ON token_usage (session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_token_usage_created
ON token_usage (created_at);
