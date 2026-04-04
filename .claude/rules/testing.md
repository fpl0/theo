---
paths: ["tests/**"]
---

# Testing conventions

- Use `bun:test` — `import { test, expect, describe, beforeEach, afterEach, mock } from "bun:test"`
- All tests live under the top-level `tests/` directory
- Construct config objects directly in tests — do not import singleton config
- Mock external I/O (database, network, SDK) — unit tests must not require infrastructure

## Patterns

- Use `describe` blocks to group related tests
- Use `beforeEach` for test isolation (reset mocks, create fresh instances)
- Prefer explicit assertions over snapshot tests
- Test event handlers with synthetic events — construct TheoEvent objects directly
- For database-dependent tests, use a test database or mock the sql tagged template
