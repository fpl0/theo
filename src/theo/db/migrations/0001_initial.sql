-- Database foundations: extensions and shared utility functions.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Shared domain types: single source of truth for constrained text columns.
-- ALTER DOMAIN updates all columns in one statement when tiers change.
CREATE DOMAIN trust_tier AS text
CHECK (value IN (
    'owner', 'owner_confirmed', 'verified',
    'inferred', 'external', 'untrusted'
));

CREATE DOMAIN sensitivity_level AS text
CHECK (value IN ('normal', 'sensitive', 'private'));

-- Trigger function for auto-updating updated_at columns.
CREATE OR REPLACE FUNCTION _set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
