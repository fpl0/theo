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
