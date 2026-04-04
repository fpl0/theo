---
name: db-migrate
description: Create a new database migration for Theo.
argument-hint: <description of the migration>
user-invocable: true
---

Create a new database migration for Theo.

## Instructions

1. Read all existing migrations in `src/db/migrations/` to understand the current schema.

2. Determine the next migration number by finding the highest existing number and incrementing by 1. Format: `NNNN_description.sql` (e.g., `0002_knowledge_graph.sql`).

3. Write the migration SQL following these rules:
   - Forward-only. No down migrations.
   - Use `IF NOT EXISTS` / `IF EXISTS` guards for DDL where appropriate.
   - Use `timestamptz` for all timestamps, never `timestamp`.
   - Use `GENERATED ALWAYS AS IDENTITY` for auto-increment primary keys.
   - Add `created_at timestamptz NOT NULL DEFAULT now()` to all tables.
   - Add `updated_at` + trigger where rows will be modified.
   - Add indexes for foreign keys.
   - Table and column names are `snake_case`.
   - Include comments explaining non-obvious design choices.

4. Run `bunx tsc --noEmit` on any TypeScript files that reference the new schema to verify types align.

5. If the migration modifies existing data or alters columns, warn about potential downtime and data loss.
