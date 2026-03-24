Run the full quality gate for Theo. Fix any issues found.

## Steps

1. Run all checks in parallel:
   - `uv run ruff format --check src/ tests/` — formatting
   - `uv run ruff check src/ tests/` — lint
   - `uv run ty check src/ tests/` — type checking
   - `uv run pytest -q` — tests

2. If any check fails:
   - Fix the issue
   - Re-run only the failing check to confirm
   - Re-run all checks once everything passes

3. Report the final status of each check.
