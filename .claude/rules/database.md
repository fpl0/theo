---
paths: ["src/theo/db/**", "src/theo/db/migrations/*.sql"]
---

# Database conventions

- **Direct asyncpg.** No ORM. Parametrized queries only (`$1`, `$2`).
- **Pool config**: `command_timeout=60s`, `max_inactive_connection_lifetime=300s`, `application_name="theo"`.
- **pgvector** codec is registered in the pool `init` callback; the extension itself is created in migration 0001.
- **Migrations** are `.sql` files named `NNNN_description.sql`. The version number is parsed from the filename prefix. All SQL must pass `sqlfluff lint`.
- **Forward-only.** No down migrations. Each migration runs in its own transaction.
- Use `IF NOT EXISTS` / `IF EXISTS` guards for DDL where appropriate.
- Use `timestamptz` for all timestamps, never `timestamp`.
- Use `GENERATED ALWAYS AS IDENTITY` for auto-increment primary keys.
- Add `created_at timestamptz NOT NULL DEFAULT now()` to all tables.
- Add `updated_at` + trigger where rows will be modified.
- Add indexes for foreign keys (PostgreSQL does NOT auto-create them).
