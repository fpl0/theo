/**
 * Integration tests for the migration runner.
 *
 * Schema is set up by `just test-db` before bun test starts. These tests verify
 * migration idempotency and state — they do NOT destructively reset
 * the database (incompatible with parallel test execution).
 *
 * Requires Docker PostgreSQL running via `just up`.
 */

import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { listMigrationFiles, migrate } from "../../src/db/migrate.ts";
import type { Pool } from "../../src/db/pool.ts";
import { createTestPool, expectMonotonicIds } from "../helpers.ts";

let pool: Pool;

beforeAll(async () => {
	pool = createTestPool();
	const connectResult = await pool.connect();
	if (!connectResult.ok) {
		throw new Error(`Test setup failed: ${connectResult.error.message}`);
	}
});

afterAll(async () => {
	if (pool) {
		await pool.end();
	}
});

describe("listMigrationFiles", () => {
	test("discovers SQL files sorted by name", async () => {
		const files = await listMigrationFiles();
		expect(files.length).toBeGreaterThanOrEqual(1);

		// Every file matches the NNNN_description.sql naming convention
		for (const file of files) {
			expect(file).toMatch(/^\d{4}_.+\.sql$/);
		}

		// Verify sort order
		expectMonotonicIds(files);
	});
});

describe("migrate", () => {
	test("idempotent — re-running applied migrations applies 0", async () => {
		const result = await migrate(pool.sql);
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.value.applied).toBe(0);
	});

	test("_migrations table tracks all applied migrations", async () => {
		const rows = await pool.sql`SELECT name FROM _migrations ORDER BY name`;
		const files = await listMigrationFiles();
		expect(rows).toHaveLength(files.length);

		// Every discovered migration file is recorded as applied
		const appliedNames = rows.map((r) => String(r["name"]));
		for (const file of files) {
			expect(appliedNames).toContain(file);
		}
	});

	test("extensions are available after migration", async () => {
		const vectorResult = await pool.sql`SELECT 1 FROM pg_extension WHERE extname = 'vector'`;
		expect(vectorResult).toHaveLength(1);

		const trgmResult = await pool.sql`SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'`;
		expect(trgmResult).toHaveLength(1);
	});
});
