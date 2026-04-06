---
paths: ["tests/**"]
---

# Testing conventions

- Use `bun:test` — `import { test, expect, describe, beforeEach, afterEach, mock } from "bun:test"`
- All tests live under the top-level `tests/` directory
- Construct config objects directly in tests — do not import singleton config
- Prefer explicit assertions over snapshot tests

## Unit tests (default)

- Mock external I/O (database, network, SDK, embedding model) — unit tests must not require infrastructure
- Use `mock()` from `bun:test` for function mocks
- Test event handlers with synthetic events — construct TheoEvent objects directly
- For database-dependent unit tests, mock the `sql` tagged template

## Integration tests

- Files in `tests/db/` or named `*.integration.test.ts` run against
  real infrastructure (Docker PostgreSQL via `just up`)
- Use a test database or dedicated schema to avoid polluting dev data
- Always call `pool.end()` in `afterAll` to prevent the test runner from hanging
- These tests require `just up` before running

## Patterns

- Use `describe` blocks to group related tests
- Use `beforeEach` for test isolation (reset mocks, create fresh instances)
- For integration tests, use a real pool with `createPool(testConfig)`
