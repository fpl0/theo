set dotenv-filename := ".env.local"

# Full quality gate: biome + tsc + tests
check: lint typecheck test

# Auto-format with biome
fmt:
    bunx biome check --write .

# Biome check (lint only)
lint:
    bunx biome check .

# TypeScript type checking
typecheck:
    bunx tsc --noEmit

# Run tests
test:
    bun test

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
