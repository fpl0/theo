---
name: db-migrate
description: Create a new database migration for Theo.
argument-hint: <description of the migration>
user-invocable: true
---

Create a new database migration for Theo.

## Instructions

1. Read all existing migrations in `src/theo/db/migrations/` to understand the current schema.

2. Determine the next migration number by finding the highest existing number and incrementing by 1. Format: `NNNN_description.sql` (e.g., `0002_add_user_table.sql`).

3. Write the migration SQL following these rules:
   - Forward-only. No down migrations.
   - Use `IF NOT EXISTS` / `IF EXISTS` guards for DDL where appropriate.
   - Always use parametrized-safe patterns (no string interpolation in SQL).
   - Add indexes for foreign keys.
   - Use `timestamptz` for all timestamps, never `timestamp`.
   - Use `GENERATED ALWAYS AS IDENTITY` for auto-increment primary keys.
   - Add `created_at timestamptz NOT NULL DEFAULT now()` to all tables.
   - Add `updated_at` + trigger where rows will be modified.
   - Include comments explaining non-obvious design choices.

4. After writing the migration file, run `uv run theo` briefly to verify it applies cleanly, then shut down.

5. If the migration modifies existing data or alters columns, warn about potential downtime and data loss.
