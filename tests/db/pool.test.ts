/**
 * Integration tests for the database connection pool.
 * Requires Docker PostgreSQL running via `just up`.
 */

import { afterAll, describe, expect, test } from "bun:test";
import type { Pool } from "../../src/db/pool.ts";
import { createPool } from "../../src/db/pool.ts";
import { testDbConfig } from "../helpers.ts";

let pool: Pool;

afterAll(async () => {
	if (pool) {
		await pool.end();
	}
});

describe("createPool", () => {
	test("connects to database", async () => {
		pool = createPool(testDbConfig);
		const result = await pool.connect();
		expect(result.ok).toBe(true);
	});

	test("executes a query", async () => {
		pool = createPool(testDbConfig);
		const rows = await pool.sql`SELECT 1 AS n`;
		expect(rows).toHaveLength(1);
		expect(rows[0]?.["n"]).toBe(1);
	});

	test("connection failure returns DB_CONNECTION_FAILED", async () => {
		const badPort = 59999;
		const badPool = createPool({
			...testDbConfig,
			DATABASE_URL: `postgresql://theo:theo@localhost:${String(badPort)}/theo`,
			DB_CONNECT_TIMEOUT: 1,
		});

		const result = await badPool.connect();
		expect(result.ok).toBe(false);
		if (!result.ok) {
			expect(result.error.code).toBe("DB_CONNECTION_FAILED");
		}

		await badPool.end();
	});

	test("end drains cleanly", async () => {
		const tempPool = createPool(testDbConfig);
		await tempPool.connect();
		await tempPool.end();
		// If we get here without hanging, the test passes
	});
});
