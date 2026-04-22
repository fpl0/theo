set dotenv-filename := ".env.local"

# Prepend mise shims so `bun` / `bunx` resolve via mise in any shell
export PATH := env_var("HOME") + "/.local/share/mise/shims:" + env_var("PATH")

# Full quality gate: biome + markdownlint + tsc + tests
check: lint mdlint typecheck test

# Auto-format with biome
fmt:
    bunx biome check --write .

# Biome check (lint only)
lint:
    bunx biome check .

# Markdown lint (strict)
mdlint:
    bunx markdownlint-cli2 "**/*.md" "#node_modules"

# TypeScript type checking
typecheck:
    bunx tsc --noEmit

# Ensure test database exists and is migrated (idempotent)
test-db:
    @docker compose exec -T postgres psql -U theo -tc \
      "SELECT 1 FROM pg_database WHERE datname = 'theo_test'" | grep -q 1 \
      || docker compose exec -T postgres psql -U theo -c "CREATE DATABASE theo_test OWNER theo"
    @DATABASE_URL=postgresql://theo:theo@localhost:5432/theo_test bun run src/db/migrate.ts

# Run tests (ensures test db is ready)
test: test-db
    bun test

# Run a specific test file (no DB dependency — use for pure unit tests)
test-file FILE:
    bun test {{FILE}}

# Start infra + agent
dev: up
    bun run src/index.ts

# Start PostgreSQL
up:
    docker compose up -d
    @echo "Waiting for PostgreSQL..."
    @until docker compose exec -T postgres pg_isready -U theo -d theo > /dev/null 2>&1; do sleep 1; done
    @echo "PostgreSQL is ready."

# Stop containers
down:
    docker compose down

# Run the migration runner scaffold
migrate:
    bun run src/db/migrate.ts

# Start the local LGTM+P observability stack (Grafana, Loki, Tempo, Prometheus, Pyroscope, collector, Promtail)
observe-up:
    docker compose -f ops/observability/docker-compose.yaml up -d
    @echo "Grafana: http://localhost:3000  (admin/admin)"

# Stop the local observability stack
observe-down:
    docker compose -f ops/observability/docker-compose.yaml down

# Install Theo as a launchd agent (creates workspace, seeds healthy_commit, loads plist)
install:
    bash ops/install.sh

# Install the launchd agent AND bring up the observability stack
install-full:
    bash ops/install.sh --with-observability

# Roll back to the healthy commit (manual escape hatch for broken self-updates)
rollback:
    #!/usr/bin/env bash
    set -euo pipefail
    WORKSPACE="${THEO_WORKSPACE:-$HOME/Theo}"
    if [ ! -f "$WORKSPACE/data/healthy_commit" ]; then
      echo "No healthy_commit recorded at $WORKSPACE/data/healthy_commit — cannot roll back" >&2
      exit 1
    fi
    HEALTHY=$(cat "$WORKSPACE/data/healthy_commit")
    echo "Resetting working tree to $HEALTHY"
    git reset --hard "$HEALTHY"
