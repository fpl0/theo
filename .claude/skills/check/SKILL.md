---
name: check
description: Run the full quality gate for Theo. Fix any issues found.
user-invocable: true
---

Run the full quality gate for Theo. Fix any issues found.

## Steps

1. Run all checks in parallel:
   - `bunx biome check .` — lint + formatting
   - `bunx tsc --noEmit` — type checking
   - `bun test` — tests

2. If any check fails:
   - Fix the issue (never suppress with biome-ignore or @ts-ignore)
   - Re-run only the failing check to confirm
   - Re-run all checks once everything passes

3. Report the final status of each check.
