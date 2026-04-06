---
name: check
description: Run the full quality gate for Theo. Fix any issues found.
user-invocable: true
---

# Quality Gate

Run the full quality gate. Fix any issues found.

## Steps

1. Run `just check` — this executes lint, typecheck, and tests in
   sequence. Sequential order matters: lint errors should be fixed
   before investigating type errors, and type errors before test
   failures.

2. If any step fails:
   - Fix the issue. Never suppress with `biome-ignore`, `@ts-ignore`, or `@ts-expect-error`.
   - Re-run `just check` from the top to confirm the fix didn't introduce new issues.
   - Repeat until the full gate passes.

3. Report the final status of each step: lint, typecheck, tests.
