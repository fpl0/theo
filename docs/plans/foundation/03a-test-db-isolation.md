# Phase 3a: Test Database Isolation

## Motivation

Integration tests and the live Theo instance share the same `theo` database. Tests destructively
modify data — dropping event partitions, deleting handler cursors, resetting tables. Running tests
while Theo is live would destroy production state.

Phase 4 adds 8 memory tables with seed data, expanding the blast radius. Isolating the test database
now prevents this class of problem permanently.

## Depends on

- **Phase 1** — Docker Compose setup, migration runner

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `docker/init-test-db.sh` | Creates `theo_test` database on fresh Docker initialization |

### Files to modify

| File | Change |
| ------ | -------- |
| `docker-compose.yml` | Mount init script to `/docker-entrypoint-initdb.d/` |
| `justfile` | Add `test-db` recipe; make `test` depend on `test-db` |
| `tests/helpers.ts` | Point `testDatabaseUrl()` at `theo_test` |
| `CLAUDE.md` | Document `just test-db` command |

## Design Decisions

### Separate database, same container

A second database (`theo_test`) in the existing PostgreSQL container. No extra Docker services, no
extra ports, no extra volumes. The same `pgvector/pgvector:pg17` image serves both databases.

Alternatives considered:

- **Separate schema** — postgres.js does not support `search_path` natively; would require raw SQL
  preamble on every connection.
- **Separate container** — overkill for a single-owner project. Wastes memory.
- **Test transactions that roll back** — does not work for migration tests or partition DDL.

### Two creation paths

PostgreSQL runs scripts in `/docker-entrypoint-initdb.d/` only on first volume initialization. For
existing setups where the volume already has data, the justfile `test-db` recipe creates the database
idempotently. Both paths converge on the same result.

### Automatic migration before tests

The `test-db` recipe runs `bun run src/db/migrate.ts` against `theo_test` after ensuring the
database exists. This means `just test` always runs against an up-to-date schema — no manual
migration step needed.

### Single change point

All integration tests import `testDbConfig` from `tests/helpers.ts`. Changing the database name in
`testDatabaseUrl()` propagates to every test file automatically. No individual test files need
modification.

## Definition of Done

- [ ] `docker/init-test-db.sh` creates `theo_test` database on fresh Docker init
- [ ] `docker-compose.yml` mounts init script to `/docker-entrypoint-initdb.d/`
- [ ] `just test-db` creates `theo_test` if it does not exist (idempotent)
- [ ] `just test-db` runs migrations against `theo_test`
- [ ] `just test` depends on `test-db` (tests always run against migrated test database)
- [ ] `tests/helpers.ts` points at `theo_test` (not `theo`)
- [ ] `just migrate` still targets the production `theo` database (unchanged)
- [ ] All existing tests pass against `theo_test`
- [ ] `just check` passes

## Test Cases

| Test | Action | Expected |
| ------ | -------- | ---------- |
| Fresh volume | `just up` after removing volume | Both `theo` and `theo_test` databases exist |
| Existing volume | `just test-db` on existing container | `theo_test` created if missing, migrations applied |
| Test isolation | Insert row into `theo`, run `just test` | Production data untouched |
| Idempotent | Run `just test-db` twice | No errors on second run |
| Migration parity | Run `just migrate` then `just test-db` | Both databases have same tables |

## Risks

**Minimal risk.** This is a test infrastructure change with no application code modifications. The
only subtlety is ensuring the Docker init script has the correct permissions and encoding (must be
executable, must use LF line endings).
