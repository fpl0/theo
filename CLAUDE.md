# Theo

Autonomous personal agent with persistent episodic and semantic memory.
Reasons, remembers, and acts through external interfaces — built for decades of continuous use.
Local-first on Apple Silicon. Async Python. Minimal dependencies, full observability.

## Quick start

Prerequisites: [just](https://github.com/casey/just) (`brew install just`).

```bash
just dev                      # start infra + agent (one command)
```

Or step by step:

```bash
just up                       # start PostgreSQL + OpenObserve
just run                      # start the agent
just down                     # stop containers
just reset                    # nuke volumes, fresh start
```

Run `just` with no arguments to see all available targets.

- **OpenObserve UI**: http://localhost:5080 (theo@theo.dev / theo)
- **PostgreSQL**: localhost:5432 (theo/theo/theo)

## Architecture

Theo is a cohesive application, not a framework. No plugin system, no abstract base classes, no dependency injection. Each module owns one concern and exposes the minimal API needed.

All code lives under `src/theo/`. Key modules by concern:

- **Entrypoint**: `__main__.py` — lifecycle, signal handling
- **Config**: `config.py` — pydantic-settings, env-driven
- **LLM**: `llm.py` — Anthropic streaming client, speed classification
- **Conversation**: `conversation/` — engine lifecycle, turn execution, context assembly
- **Memory**: `memory/` — knowledge graph (nodes, episodes, core ops, LLM tools)
- **Database**: `db/` — asyncpg pool, forward-only SQL migrations
- **Gates**: `gates/` — external interfaces (Telegram via aiogram)
- **Resilience**: `resilience/` — circuit breaker, retry queue, health check
- **Infra**: `bus/` (event bus), `telemetry.py` (OTEL), `embeddings.py` (MLX BERT), `errors.py` (exception hierarchy)

## Design decisions

- **Built for decades.** Theo is a personal agent designed for 10+ years of continuous use. Every decision — schema design, data model, migration strategy, dependency choices — must prioritize long-term durability. Avoid anything that creates future migration pain: prefer stable formats, keep data portable, and never lock into abstractions that age poorly.
- **Minimal dependencies.** Every dependency must earn its place. No ORMs, no web frameworks for the sake of it. asyncpg over SQLAlchemy, raw SQL over query builders.
- **Astral tooling only.** uv for packages, ruff for linting/formatting, ty for type checking. No mypy, no black, no isort.
- **Zero lint/type tolerance.** `ruff check`, `ty check`, and `sqlfluff lint` must pass with zero errors. `ruff` selects ALL rules. `ty` sets all rules to error. `sqlfluff` targets PostgreSQL dialect.
- **Async-native.** asyncio throughout. Heavy sync work (MLX inference) runs via `asyncio.to_thread`.
- **Strict typing.** `Literal` for constrained strings, `SecretStr` for credentials, `model_validator` for relational constraints. No unvalidated config.
- **Observability is non-negotiable.** Every operation must be traceable. If you can't see it in OpenObserve, it doesn't exist. All three OTEL signals (traces, metrics, logs) are required. Every public function that does I/O must create a span. Every meaningful state change must be logged. Key operations must emit metrics. Code without observability is incomplete code — treat missing spans like missing error handling.
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
- When ty cannot model a pattern (pydantic-settings constructors, SDK discriminated unions), add a targeted `[[tool.ty.overrides]]` in `pyproject.toml` scoped to the specific file and rule. Never blanket-ignore.

### Configuration

All config is in `Settings` (`config.py`), loaded from `THEO_*` env vars.
Non-Theo env vars (`OTEL_*`) are loaded via `dotenv` in `__main__.py`.
Files: `.env` (shared defaults) → `.env.local` (local overrides, gitignored).

### Database

See `.claude/rules/database.md`.

### Telemetry

See `.claude/rules/telemetry.md`.

### Testing

See `.claude/rules/testing.md`.

```bash
just check                             # full quality gate (fail-fast)
just lint                              # lint + typecheck only (no tests)
just test                              # run tests only
just fmt                               # auto-format python + sql
```

### Infrastructure

- **PostgreSQL 18 + pgvector 0.8.2**: knowledge graph with vector + full-text search.
- **OpenObserve v0.70.0**: lightweight OTEL backend (~250MB RAM). Traces, metrics, logs in one binary.
- All infrastructure runs via `docker compose`. Data is persisted in named volumes.

## Decision records

Architectural decisions are documented in `docs/decisions/`. Each file covers one change: context, rationale, and files changed. Check relevant decision docs before modifying a module to understand why it was built the way it was.

Any code change that introduces a new module, changes architecture, or makes a non-obvious design choice **must** include a decision record. When modifying existing modules, update the corresponding decision doc if the rationale or file list changes. Files are named `kebab-case-topic.md` and follow the structure: Context, Decisions (with rationale per choice), Files changed.

## Adding new modules

1. Create the module in `src/theo/`
2. Add a logger: `log = logging.getLogger(__name__)`
3. Add a tracer: `tracer = trace.get_tracer(__name__)`
4. Wrap every public I/O function in `tracer.start_as_current_span()`
5. Add custom exceptions to `errors.py` if needed
6. Write tests in `tests/test_<module>.py`
7. Add or update the decision record in `docs/decisions/`
8. Run `just check`
9. Verify spans appear in OpenObserve (`THEO_OTEL_EXPORTER=otlp`)

## Claude Code configuration

The `.claude/` directory contains all Claude Code tooling for this project:

- **Skills** (`.claude/skills/`): `/check`, `/db-migrate`, `/db-optimize`, `/otel-check`, `/otel-dashboard`
- **Subagents** (`.claude/agents/`): `code-reviewer` — expert review for agentic AI, async Python, and PostgreSQL
- **Rules** (`.claude/rules/`): path-specific conventions loaded only when editing relevant files — `telemetry.md`, `database.md`, `testing.md`
- **Hooks** (`.claude/settings.json`): auto-format Python (`ruff`) and SQL (`sqlfluff`) on every edit
- **Local overrides** (`.claude/settings.local.json`): machine-specific permissions (gitignored)
