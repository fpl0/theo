/**
 * Integration tests for the migration runner.
 * Requires Docker PostgreSQL running via `just up`.
 */

import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { listMigrationFiles, migrate } from "../../src/db/migrate.ts";
import type { Pool } from "../../src/db/pool.ts";
import { createPool } from "../../src/db/pool.ts";
import { testDbConfig } from "../helpers.ts";

let pool: Pool;

beforeAll(async () => {
	pool = createPool(testDbConfig);
	const connectResult = await pool.connect();
	if (!connectResult.ok) {
		throw new Error(`Test setup failed: ${connectResult.error.message}`);
	}

	// Clean slate: drop _migrations table so tests are idempotent
	await pool.sql`DROP TABLE IF EXISTS _migrations`;
	// Also drop extensions so migration can re-apply them
	await pool.sql`DROP EXTENSION IF EXISTS pg_trgm`;
	await pool.sql`DROP EXTENSION IF EXISTS vector`;
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
		expect(files).toContain("0001_extensions.sql");

		// Verify sort order
		for (let i = 1; i < files.length; i++) {
			const prev = files[i - 1];
			const curr = files[i];
			if (prev !== undefined && curr !== undefined) {
				expect(prev < curr).toBe(true);
			}
		}
	});
});

describe("migrate", () => {
	test("creates _migrations table and applies new migration", async () => {
		const result = await migrate(pool.sql);
		expect(result.ok).toBe(true);
		if (!result.ok) {
			return;
		}

		expect(result.value.applied).toBeGreaterThanOrEqual(1);

		// Verify _migrations table exists and has entries
		const rows = await pool.sql`SELECT name FROM _migrations ORDER BY name`;
		expect(rows.length).toBeGreaterThanOrEqual(1);
		expect(rows[0]?.["name"]).toBe("0001_extensions.sql");
	});

	test("skips already-applied migration (idempotent)", async () => {
		// Second run -- everything already applied
		const result = await migrate(pool.sql);
		expect(result.ok).toBe(true);
		if (!result.ok) {
			return;
		}

		expect(result.value.applied).toBe(0);
	});

	test("extensions are available after migration", async () => {
		// Verify pgvector extension is loaded
		const vectorResult = await pool.sql`SELECT 1 FROM pg_extension WHERE extname = 'vector'`;
		expect(vectorResult).toHaveLength(1);

		// Verify pg_trgm extension is loaded
		const trgmResult = await pool.sql`SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'`;
		expect(trgmResult).toHaveLength(1);
	});
});
