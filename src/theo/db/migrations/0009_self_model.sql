-- Self model: tracks Theo's accuracy per domain for calibration.
--
-- Seeded with initial domains.  Actual scoring and calibration curves
-- come in M5; this migration provides the schema and seed data.

CREATE TABLE IF NOT EXISTS self_model_domain (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    domain text NOT NULL UNIQUE,
    accuracy real
    CONSTRAINT smd_accuracy_range
    CHECK (accuracy IS NULL OR (accuracy >= 0.0 AND accuracy <= 1.0)),
    total_predictions integer NOT NULL DEFAULT 0
    CONSTRAINT smd_total_nonneg CHECK (total_predictions >= 0),
    correct_predictions integer NOT NULL DEFAULT 0
    CONSTRAINT smd_correct_nonneg CHECK (correct_predictions >= 0),
    meta jsonb NOT NULL DEFAULT '{}',
    last_evaluated_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT smd_correct_le_total
    CHECK (correct_predictions <= total_predictions)
);

CREATE OR REPLACE TRIGGER trg_smd_updated_at
BEFORE UPDATE ON self_model_domain
FOR EACH ROW
EXECUTE FUNCTION _set_updated_at();

-- Seed initial domains.
INSERT INTO self_model_domain (domain) VALUES
('scheduling'),
('drafting'),
('recommendations'),
('research'),
('summarization'),
('task_execution')
ON CONFLICT (domain) DO NOTHING;
