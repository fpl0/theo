---
paths: ["tests/**"]
---

# Testing conventions

- pytest-asyncio in auto mode. All async tests just work.
- Tests construct `Settings(...)` directly, never via `get_settings()` (which is cached).
- Use `_env_file=None` when testing Settings to isolate from `.env.local`.

## Commands

```bash
just check                             # full quality gate (fail-fast)
just lint                              # lint + typecheck only (no tests)
just test                              # run tests only
just fmt                               # auto-format python + sql
```

Underlying commands (for reference / CI):

```bash
uv run pytest                          # run all tests
uv run ruff check src/ tests/          # lint python
uv run ruff format --check src/ tests/ # check python formatting
uv run ty check src/ tests/            # type check
uv run sqlfluff lint src/              # lint sql
```
