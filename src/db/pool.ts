/**
 * postgres.js connection pool factory.
 *
 * createPool() returns a Pool object without connecting -- postgres.js
 * establishes connections lazily on first query. Use pool.connect() for
 * an eager health check at startup.
 *
 * Every test file that creates a pool must call pool.end() in afterAll
 * to prevent the test runner from hanging.
 */

import postgres from "postgres";
import type { DbConfig } from "../config.ts";
import type { AppError, Result } from "../errors.ts";
import { err, ok } from "../errors.ts";

export type { Sql } from "postgres";

/** Maximum connection lifetime in seconds (30 minutes). */
const MAX_LIFETIME_SECONDS = 1800;

/** Bundled pool interface: the sql tagged template + lifecycle methods. */
export interface Pool {
	/** The postgres.js tagged template. All queries go through this. */
	readonly sql: postgres.Sql;
	/** Eagerly connect to verify the database is reachable. Returns Result. */
	connect(): Promise<Result<void, AppError>>;
	/** Drain connections. Call in shutdown and test teardown. */
	end(): Promise<void>;
}

/**
 * Create a connection pool from database configuration.
 *
 * Does NOT connect immediately -- postgres.js connects on first query.
 * Call pool.connect() to verify connectivity at startup.
 */
export function createPool(config: DbConfig): Pool {
	const sql = postgres(config.DATABASE_URL, {
		max: config.DB_POOL_MAX,
		idle_timeout: config.DB_IDLE_TIMEOUT,
		connect_timeout: config.DB_CONNECT_TIMEOUT,
		max_lifetime: MAX_LIFETIME_SECONDS,
	});

	return {
		sql,
		async connect(): Promise<Result<void, AppError>> {
			try {
				await sql`SELECT 1`;
				return ok(undefined);
			} catch (e: unknown) {
				return err({
					code: "DB_CONNECTION_FAILED" as const,
					message: e instanceof Error ? e.message : String(e),
				});
			}
		},
		async end(): Promise<void> {
			await sql.end();
		},
	};
}
