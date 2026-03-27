-- Structured user model: tracked dimensions with confidence scoring.

CREATE TABLE IF NOT EXISTS user_model_dimension (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    framework text NOT NULL,
    dimension text NOT NULL,
    value jsonb NOT NULL DEFAULT '{}',
    confidence real NOT NULL DEFAULT 0.0
    CONSTRAINT umd_confidence_range
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    evidence_count integer NOT NULL DEFAULT 0
    CONSTRAINT umd_evidence_positive CHECK (evidence_count >= 0),
    meta jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT umd_framework_dimension_unique UNIQUE (framework, dimension)
);

CREATE OR REPLACE TRIGGER trg_umd_updated_at
BEFORE UPDATE ON user_model_dimension
FOR EACH ROW EXECUTE FUNCTION _set_updated_at();

CREATE INDEX IF NOT EXISTS ix_umd_framework
ON user_model_dimension (framework);

-- Seed canonical dimensions (29 total).
-- All start at confidence=0.0, evidence_count=0, empty value.

-- Schwartz values (10)
INSERT INTO user_model_dimension (framework, dimension) VALUES
('schwartz', 'self_direction'),
('schwartz', 'stimulation'),
('schwartz', 'hedonism'),
('schwartz', 'achievement'),
('schwartz', 'power'),
('schwartz', 'security'),
('schwartz', 'conformity'),
('schwartz', 'tradition'),
('schwartz', 'benevolence'),
('schwartz', 'universalism')
ON CONFLICT ON CONSTRAINT umd_framework_dimension_unique DO NOTHING;

-- Big Five personality traits (5)
INSERT INTO user_model_dimension (framework, dimension) VALUES
('big_five', 'openness'),
('big_five', 'conscientiousness'),
('big_five', 'extraversion'),
('big_five', 'agreeableness'),
('big_five', 'neuroticism')
ON CONFLICT ON CONSTRAINT umd_framework_dimension_unique DO NOTHING;

-- Narrative identity (3)
INSERT INTO user_model_dimension (framework, dimension) VALUES
('narrative', 'identity_themes'),
('narrative', 'turning_points'),
('narrative', 'future_story')
ON CONFLICT ON CONSTRAINT umd_framework_dimension_unique DO NOTHING;

-- Communication preferences (4)
INSERT INTO user_model_dimension (framework, dimension) VALUES
('communication', 'verbosity'),
('communication', 'formality'),
('communication', 'emoji_tolerance'),
('communication', 'preferred_format')
ON CONFLICT ON CONSTRAINT umd_framework_dimension_unique DO NOTHING;

-- Energy patterns (3)
INSERT INTO user_model_dimension (framework, dimension) VALUES
('energy', 'peak_hours'),
('energy', 'wind_down_hours'),
('energy', 'timezone')
ON CONFLICT ON CONSTRAINT umd_framework_dimension_unique DO NOTHING;

-- Goals (2)
INSERT INTO user_model_dimension (framework, dimension) VALUES
('goals', 'active_goals'),
('goals', 'completed_goals')
ON CONFLICT ON CONSTRAINT umd_framework_dimension_unique DO NOTHING;

-- Boundaries (2)
INSERT INTO user_model_dimension (framework, dimension) VALUES
('boundaries', 'never_do'),
('boundaries', 'sensitivity_preferences')
ON CONFLICT ON CONSTRAINT umd_framework_dimension_unique DO NOTHING;
