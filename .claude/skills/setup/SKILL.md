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
  5. Telegram          — bot setup (optional)
  6. Verification      — quality gate + connectivity
```

## Phase 1 — Prerequisites

Check and install:

| Tool | Check | Install |
|------|-------|---------|
| Bun | `bun --version` | `curl -fsSL https://bun.sh/install \| bash` |
| Docker | `docker --version` | Ask user to install Docker Desktop |
| just | `just --version` | `brew install just` |

## Phase 2 — Dependencies

```bash
bun install
```

## Phase 3 — Infrastructure

Check if PostgreSQL container is running. If not, start via docker compose.
Wait for health checks (poll every 2s, up to 30s).

## Phase 4 — Configuration

Build `.env.local` incrementally (do not overwrite existing values):

- `DATABASE_URL=postgresql://theo:theo@localhost:5432/theo`
- `ANTHROPIC_API_KEY` — ask user if not present

## Phase 5 — Telegram (optional)

Ask the user if they want Telegram integration. If yes:
1. Walk through BotFather bot creation
2. Get the bot token
3. Discover owner chat ID
4. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_ID` to `.env.local`

## Phase 6 — Verification

- Database connectivity check
- `bunx biome check .` + `bunx tsc --noEmit`
- Brief smoke test

Print final summary with key commands.
