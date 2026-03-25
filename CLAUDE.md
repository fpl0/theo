# Theo

Personal AI agent. Apple Silicon, local-first, async Python.

## Quick start

```bash
docker compose up -d          # PostgreSQL + OpenObserve
uv run theo                   # starts the agent
```

- **OpenObserve UI**: http://localhost:5080 (theo@theo.dev / theo)
- **PostgreSQL**: localhost:5432 (theo/theo/theo)

## Architecture

Theo is a cohesive application, not a framework. No plugin system, no abstract base classes, no dependency injection. Each module owns one concern and exposes the minimal API needed.

```
src/theo/
  __main__.py      # entrypoint — lifecycle, signal handling
  config.py        # pydantic-settings, env-driven, validated
  errors.py        # exception hierarchy (TheoError base)
  telemetry.py     # OTEL bootstrap — traces, metrics, logs
  embeddings.py    # MLX BERT on Apple Silicon, async API
  db/
    __init__.py    # singleton Database instance
    pool.py        # asyncpg pool lifecycle
    migrate.py     # versioned SQL migrations
    migrations/    # numbered .sql files (0001_initial.sql, ...)
```

## Design decisions

- **Minimal dependencies.** Every dependency must earn its place. No ORMs, no web frameworks for the sake of it. asyncpg over SQLAlchemy, raw SQL over query builders.
- **Astral tooling only.** uv for packages, ruff for linting/formatting, ty for type checking. No mypy, no black, no isort.
- **Zero lint/type tolerance.** `ruff check`, `ty check`, and `sqlfluff lint` must pass with zero errors. `ruff` selects ALL rules. `ty` sets all rules to error. `sqlfluff` targets PostgreSQL dialect.
- **Async-native.** asyncio throughout. Heavy sync work (MLX inference) runs via `asyncio.to_thread`.
- **Strict typing.** `Literal` for constrained strings, `SecretStr` for credentials, `model_validator` for relational constraints. No unvalidated config.
- **OpenTelemetry everywhere.** All three signals (traces, metrics, logs). stdlib logging is bridged to OTEL. asyncpg is auto-instrumented. Add `tracer.start_as_current_span()` to new operations.
- **SQL migrations are forward-only.** Numbered files in `db/migrations/`. No down migrations. Each runs in its own transaction.
- **Module-level singletons.** `db`, `embedder`, `get_settings()` are initialized once. No factory patterns.
- **Custom exceptions over RuntimeError.** All Theo errors inherit from `TheoError` in `errors.py`.

## Conventions

### Code style

- Python 3.14+, use modern syntax (union types with `|`, `type` statements)
- `from __future__ import annotations` when using `TYPE_CHECKING` guards
- Loggers: `log = logging.getLogger(__name__)`
- Tracers: `tracer = trace.get_tracer(__name__)`
- Keep modules focused. If a file exceeds ~200 lines, split it.

### Configuration

All config is in `Settings` (`config.py`), loaded from `THEO_*` env vars.
Non-Theo env vars (`OTEL_*`) are loaded via `dotenv` in `__main__.py`.
Files: `.env` (shared defaults) → `.env.local` (local overrides, gitignored).

### Database

- **Direct asyncpg.** No ORM. Parametrized queries only (`$1`, `$2`).
- **Pool config**: `command_timeout=60s`, `max_inactive_connection_lifetime=300s`, `application_name="theo"`.
- **pgvector** extension is created during pool init, before migrations run.
- **Migrations** are `.sql` files named `NNNN_description.sql`. The version number is parsed from the filename prefix. All SQL must pass `sqlfluff lint`.

### Telemetry

- `init_telemetry()` bootstraps all signals. `shutdown_telemetry()` flushes on exit.
- Exporter is chosen via `THEO_OTEL_EXPORTER`: `"console"` (dev) or `"otlp"` (production).
- OTLP exports to OpenObserve at `http://localhost:5080/api/default` with Basic auth.
- asyncpg queries use `sanitize_query=True` to avoid leaking parameters in spans.
- New modules should create a tracer and add spans to meaningful operations.

### Testing

```bash
uv run pytest                          # run all tests
uv run ruff check src/                 # lint python
uv run ty check src/                   # type check
uv run sqlfluff lint src/              # lint sql
uv run sqlfluff fix src/               # format sql
```

- pytest-asyncio in auto mode. All async tests just work.
- Tests construct `Settings(...)` directly, never via `get_settings()` (which is cached).
- Use `_env_file=None` when testing Settings to isolate from `.env.local`.

### Infrastructure

- **PostgreSQL 17 + pgvector 0.8.2**: knowledge graph with vector + full-text search.
- **OpenObserve v0.70.0**: lightweight OTEL backend (~250MB RAM). Traces, metrics, logs in one binary.
- All infrastructure runs via `docker compose`. Data is persisted in named volumes.

## Adding new modules

1. Create the module in `src/theo/`
2. Add a logger: `log = logging.getLogger(__name__)`
3. Add a tracer if the module does I/O: `tracer = trace.get_tracer(__name__)`
4. Add custom exceptions to `errors.py` if needed
5. Write tests in `tests/test_<module>.py`
6. Run `uv run ruff check src/ && uv run ty check src/ && uv run sqlfluff lint src/ && uv run pytest`
