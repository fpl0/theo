/**
 * Test conventions — read this before writing tests.
 *
 * 1. Run tests via `just test` (not `bun test` directly).
 *    `just test` ensures theo_test exists and migrations are applied.
 *
 * 2. Test files NEVER touch schema — no migrate(), no DROP TABLE, no DROP EXTENSION.
 *    Schema is managed by `just test-db` before bun test starts.
 *
 * 3. Test files clean their own DATA in beforeEach/beforeAll:
 *    - Use cleanEventTables() from helpers.ts for event-related cleanup
 *    - Use DELETE FROM for memory tables
 *
 * 4. Use testDbConfig from helpers.ts for all pool connections.
 *
 * 5. Always call pool.end() in afterAll to prevent hanging.
 */
