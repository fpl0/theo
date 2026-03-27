# Theo dev commands — run `just` to see all targets.

set dotenv-load

# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

# Start PostgreSQL + OpenObserve
up:
    docker compose up -d

# Stop all containers
down:
    docker compose down

# Nuke volumes and restart fresh
reset:
    docker compose down -v
    docker compose up -d
    just dashboards
    @echo "Volumes destroyed — database is empty, dashboards provisioned."

# Show container status
status:
    docker compose ps

# Tail container logs (just logs, or just logs postgres)
logs service="":
    docker compose logs -f {{ service }}

# Open a psql shell
psql:
    docker compose exec postgres psql -U theo theo

# Provision OpenObserve dashboards + alerts
dashboards:
    infra/provision.sh

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

# Start the agent
run:
    uv run theo

# Start infra, provision dashboards, then run the agent
dev: up dashboards
    uv run theo

# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------

# Full quality gate — lint + typecheck + test (fail-fast)
check:
    uv run ruff check src/
    uv run ruff format --check src/
    uv run sqlfluff lint src/
    uv run ty check src/
    uv run pytest

# Lint + typecheck only (no tests)
lint:
    uv run ruff check src/
    uv run ruff format --check src/
    uv run sqlfluff lint src/
    uv run ty check src/

# Run tests
test:
    uv run pytest

# Auto-format Python and SQL
fmt:
    uv run ruff check --fix src/
    uv run ruff format src/
    uv run sqlfluff fix src/

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

# Remove tool caches
clean:
    rm -rf .ruff_cache .pytest_cache .ty .coverage htmlcov
    find src tests -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
