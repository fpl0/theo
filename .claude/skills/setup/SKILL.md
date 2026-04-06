---
name: setup
description: First-time Theo setup. Install dependencies, configure infrastructure, and get Theo running.
user-invocable: true
---

# Theo Setup

Get the user from zero to a running Theo instance.

## Principles

- Run commands yourself — do not tell the user to run things.
- Fix problems yourself — only escalate when it requires user action (credentials, external accounts).
- Detect existing state. Before each step, check if it's already done. Skip silently if so.

## Phase 0 — Welcome

Print:

```text
Theo Setup
----------
Phases:
  1. Prerequisites     — Bun, Docker, just
  2. Dependencies      — bun install
  3. Infrastructure    — PostgreSQL + pgvector
  4. Configuration     — API keys + environment
  5. Migrations        — run migration runner
  6. Verification      — quality gate + connectivity
```

## Phase 1 — Prerequisites

Check and install:

| Tool   | Check              | Install                                        |
| ------ | ------------------ | ---------------------------------------------- |
| Bun    | `bun --version`    | `curl -fsSL https://bun.sh/install \| bash`    |
| Docker | `docker --version` | Ask user to install Docker Desktop             |
| just   | `just --version`   | `brew install just`                            |

## Phase 2 — Dependencies

```bash
bun install
```

## Phase 3 — Infrastructure

Check if PostgreSQL container is running. If not, start via `just up`.
Wait for health checks (poll every 2s, up to 30s).

## Phase 4 — Configuration

Build `.env.local` incrementally (do not overwrite existing values):

- `DATABASE_URL=postgresql://theo:theo@localhost:5432/theo`
- `ANTHROPIC_API_KEY` — ask user if not present

Optional (only if user requests):

- `TELEGRAM_BOT_TOKEN` — for future Telegram gate
- `TELEGRAM_OWNER_ID` — for future Telegram gate

## Phase 5 — Migrations

Run the migration runner to apply any pending migrations:

```bash
just migrate
```

Verify the `_migrations` table exists and extensions are applied:

- `vector` extension (pgvector)
- `pg_trgm` extension (trigram similarity)

## Phase 6 — Verification

Run the full quality gate:

```bash
just check
```

This runs biome (lint + format), tsc (type checking), and bun test (tests) in sequence.

Also verify database connectivity directly:

- Confirm `just migrate` reports 0 pending migrations (idempotent re-run)

Print final summary:

```text
Theo is ready.

Key commands:
  just check    — full quality gate (biome + tsc + tests)
  just dev      — start infra + agent
  just up       — start PostgreSQL
  just down     — stop containers
  just migrate  — run migrations
```
