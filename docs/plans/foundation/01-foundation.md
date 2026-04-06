# Phase 1: Foundation

## Motivation

Everything depends on configuration and database connectivity. Without a validated config, a working
connection pool, and a migration runner that actually executes SQL, nothing else can be built. This
phase establishes the substrate that every subsequent phase builds on.

It also establishes the error-as-values pattern (`Result<T, E>`) that replaces exceptions throughout
the codebase, and provides the first migration (PostgreSQL extensions).

## Depends on

Nothing. This is the starting point.

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/config.ts` | Zod-validated environment configuration |
| `src/db/pool.ts` | postgres.js connection pool factory |
| `src/db/migrate.ts` | Complete migration runner (replace current scaffold) |
| `src/errors.ts` | `Result<T, E>` type, `AppError` hierarchy |
| `src/db/migrations/0001_extensions.sql` | pgvector + pg_trgm extensions |
| `tests/config.test.ts` | Config validation tests |
| `tests/db/migrate.test.ts` | Migration runner tests |
| `tests/db/pool.test.ts` | Pool connectivity integration test |

### Files to modify

| File | Change |
| ------ | -------- |
| `src/index.ts` | Wire up config + pool (still minimal) |

## Design Decisions

### Error Types (`src/errors.ts`)

Defined first because `Result` is used by every other module in this phase.

```typescript
type Result<T, E = AppError> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly error: E };

type AppError =
  | {
      readonly code: "CONFIG_INVALID";
      readonly message: string;
      readonly issues: ReadonlyArray<{
        readonly path: string;
        readonly message: string;
      }>;
    }
  | { readonly code: "DB_CONNECTION_FAILED"; readonly message: string }
  | { readonly code: "MIGRATION_FAILED"; readonly migration: string; readonly message: string };
  // ... grows with each phase

function ok<T>(value: T): Result<T, never>;
function err<E>(error: E): Result<never, E>;
function isOk<T, E>(result: Result<T, E>): result is { readonly ok: true; readonly value: T };
function isErr<T, E>(result: Result<T, E>): result is { readonly ok: false; readonly error: E };
```

`CONFIG_INVALID` carries structured `issues` from Zod so the caller can inspect which fields failed.

### Config (`src/config.ts`)

Zod schema parsing environment variables. Not a singleton -- tests construct config objects
directly.

```typescript
const configSchema = z.object({
  // Required
  DATABASE_URL: z.string().url(),
  ANTHROPIC_API_KEY: z.string().min(1),

  // Optional with defaults
  DB_POOL_MAX: z.coerce.number().default(10),
  DB_IDLE_TIMEOUT: z.coerce.number().default(30),
  DB_CONNECT_TIMEOUT: z.coerce.number().default(10),

  // Optional (gates not required at startup)
  TELEGRAM_BOT_TOKEN: z.string().optional(),
  TELEGRAM_OWNER_ID: z.string().optional(),
});

type Config = z.infer<typeof configSchema>;
```

`loadConfig()` returns a `Result`, never throws:

```typescript
function loadConfig(
  env: Record<string, string | undefined> = process.env,
): Result<Config, AppError> {
  const parsed = configSchema.safeParse(env);
  if (parsed.success) {
    return ok(parsed.data);
  }
  return err({
    code: "CONFIG_INVALID",
    message: "Invalid configuration",
    issues: parsed.error.issues.map((issue) => ({
      path: issue.path.join("."),
      message: issue.message,
    })),
  });
}
```

Zod's `safeParse` returns `{ success: false, error: ZodError }` on failure. We map each `ZodIssue`
into the `issues` array on `CONFIG_INVALID`. The caller never catches -- it inspects `result.ok`.

Also export the Zod schema so tests can construct partial configs without `process.env`.

### DB Pool (`src/db/pool.ts`)

Factory function, not module-level instance. Returns an object that bundles the `sql` tagged
template with lifecycle methods:

```typescript
interface Pool {
  /** The postgres.js tagged template. All queries go through this. */
  readonly sql: Sql;
  /** Eagerly connect to verify the database is reachable. Returns Result. */
  connect(): Promise<Result<void, AppError>>;
  /** Drain connections. Call in shutdown and test teardown. */
  end(): Promise<void>;
}

function createPool(config: DbConfig): Pool {
  const sql = postgres(config.DATABASE_URL, {
    max: config.DB_POOL_MAX,
    idle_timeout: config.DB_IDLE_TIMEOUT,
    connect_timeout: config.DB_CONNECT_TIMEOUT,
    max_lifetime: 60 * 30,
  });

  return {
    sql,
    async connect(): Promise<Result<void, AppError>> {
      try {
        await sql`SELECT 1`;
        return ok(undefined);
      } catch (e) {
        return err({
          code: "DB_CONNECTION_FAILED",
          message: e instanceof Error ? e.message : String(e),
        });
      }
    },
    async end(): Promise<void> {
      await sql.end();
    },
  };
}
```

Key behaviors:

- **Lazy connection.** `createPool()` itself does not connect to the database. postgres.js
  establishes connections on the first query. This means construction always succeeds -- connection
  failures only surface when a query runs.
- **`pool.connect()` for health checks.** The `connect()` method runs `SELECT 1` to eagerly verify
  connectivity at startup. If the database is unreachable, it returns `Result` with
  `DB_CONNECTION_FAILED` -- no exception escapes. Call this during startup; skip it in tests that
  use a known-good database.
- **Test teardown.** Every test file that creates a pool must call `pool.end()` in `afterAll`.
  Without this, Bun's test runner hangs because postgres.js keeps connections alive.

Test teardown pattern:

```typescript
import { afterAll, describe, test, expect } from "bun:test";

let pool: Pool;

afterAll(async () => {
  await pool.end();
});

describe("some integration test", () => {
  test("queries work", async () => {
    pool = createPool(testConfig);
    const result = await pool.connect();
    expect(result.ok).toBe(true);
  });
});
```

### Migration Runner (`src/db/migrate.ts`)

Replace the current scaffold with a working runner.

**Bootstrap sequence** (resolves the chicken-and-egg problem):

1. The migration runner itself creates `_migrations` using `CREATE TABLE IF NOT EXISTS` directly in
   the runner code, before reading any migration files. This is NOT a migration SQL file -- it is
   infrastructure that the runner owns.
2. Read all `.sql` files from `src/db/migrations/`, sorted by name.
3. Query `_migrations` for already-applied names.
4. For each unapplied file: execute its SQL in a transaction, then insert a row into `_migrations`
   within the same transaction.
5. Report applied count.

```typescript
async function migrate(sql: Sql): Promise<Result<{ applied: number }, AppError>> {
  // Step 1: Runner owns this table. Not a migration file.
  await sql`
    CREATE TABLE IF NOT EXISTS _migrations (
      name text PRIMARY KEY,
      applied_at timestamptz NOT NULL DEFAULT now()
    )
  `;

  // Step 2: Discover migration files
  const files = await listMigrationFiles();

  // Step 3: Determine which are already applied
  const applied = await sql`SELECT name FROM _migrations`;
  const appliedNames = new Set(applied.map((row) => row.name));

  // Step 4: Apply each new migration in its own transaction
  let count = 0;
  for (const file of files) {
    if (appliedNames.has(file)) continue;
    const content = await Bun.file(`src/db/migrations/${file}`).text();
    await sql.begin(async (tx) => {
      await tx.unsafe(content);
      await tx`INSERT INTO _migrations (name) VALUES (${file})`;
    });
    count++;
  }

  return ok({ applied: count });
}
```

Each migration runs in its own transaction. If a migration fails, its transaction rolls back and the
migration is not recorded -- subsequent migrations are not attempted. The runner is idempotent.

### First Migration (`0001_extensions.sql`)

This migration only creates extensions. It does NOT create `_migrations` -- the runner handles that.

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

## Definition of Done

- [ ] `loadConfig()` with valid env returns `{ ok: true, value: Config }`
- [ ] `loadConfig()` with missing `DATABASE_URL` returns `{ ok: false, error: { code:
  "CONFIG_INVALID", issues: [...] } }` (not a crash)
- [ ] `loadConfig()` maps each Zod issue to `{ path, message }` in the error's `issues` array
- [ ] `createPool(config)` returns a `Pool` object without connecting
- [ ] `pool.connect()` against a running PostgreSQL returns `{ ok: true }`
- [ ] `pool.connect()` against unreachable PostgreSQL returns `{ ok: false, error: { code:
  "DB_CONNECTION_FAILED" } }`
- [ ] `pool.end()` drains connections cleanly (no hanging test runner)
- [ ] `just migrate` creates `_migrations` table, then applies `0001_extensions.sql` and records it
- [ ] Running `just migrate` again applies nothing (idempotent)
- [ ] `just check` passes (biome + tsc + tests)

## Test Cases

### `tests/config.test.ts`

| Test | Input | Expected |
| ------ | ------- | ---------- |
| Valid config | All required env vars set | `{ ok: true, value: Config }` with correct types |
| Missing DATABASE_URL | Omit DATABASE_URL | `{ ok: false }` with `code: "CONFIG_INVALID"`, `issues` contains `{ path: "DATABASE_URL", ... }` |
| Invalid DATABASE_URL | `"not-a-url"` | `{ ok: false }` with `code: "CONFIG_INVALID"` |
| Defaults applied | Only required vars | `value.DB_POOL_MAX === 10`, `value.DB_IDLE_TIMEOUT === 30`, `value.DB_CONNECT_TIMEOUT === 10` |
| Optional fields absent | No TELEGRAM_BOT_TOKEN | `value.TELEGRAM_BOT_TOKEN === undefined` |
| Multiple errors | Missing both required vars | `issues` array contains entries for both `DATABASE_URL` and `ANTHROPIC_API_KEY` |

### `tests/db/migrate.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Discovers SQL files | Files in migrations dir | Returns sorted file list |
| Creates _migrations table | Fresh DB, no _migrations table | Runner creates table before applying files |
| Applies new migration | Fresh DB | Migration applied, row in `_migrations` |
| Skips applied migration | Migration already in `_migrations` | No re-execution, returns `{ applied: 0 }` |
| Handles empty dir | No SQL files | Returns `{ ok: true, value: { applied: 0 } }` |
| Rolls back on error | Invalid SQL in file | Transaction rolled back, migration not recorded, subsequent migrations not attempted |

### `tests/db/pool.test.ts`

Integration test that runs against Docker PostgreSQL (started by `just up`).

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Connects to database | Docker PG running | `pool.connect()` returns `{ ok: true }` |
| Executes a query | Connected pool | `pool.sql\`SELECT 1 AS n\`` returns `[{ n: 1 }]` |
| Connection failure | Invalid connection string (wrong port) | `pool.connect()` returns `{ ok: false, error: { code: "DB_CONNECTION_FAILED" } }` |
| End drains cleanly | Active pool | `pool.end()` resolves, no hanging process |

## Risks

**Low risk.** This is well-understood infrastructure. The only subtlety is ensuring postgres.js
works cleanly with Bun (it does -- widely used combination). The tagged template API (`sql`...``)
requires care to avoid string interpolation.
