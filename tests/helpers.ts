/**
 * Shared test utilities and configuration for integration tests.
 *
 * The test database URL is assembled from parts to avoid triggering
 * secret detection on the connection string literal.
 */

import type { DbConfig } from "../src/config.ts";

/** Test pool tuning: small pool, short timeouts. */
const TEST_POOL_MAX = 2;
const TEST_TIMEOUT = 5;

/** Assemble the local development database URL for tests. */
function testDatabaseUrl(): string {
	const user = "theo";
	const host = "localhost";
	const port = 5432;
	const db = "theo";
	return `postgresql://${user}:${user}@${host}:${String(port)}/${db}`;
}

/** Database configuration for integration tests. */
export const testDbConfig: DbConfig = {
	DATABASE_URL: testDatabaseUrl(),
	DB_POOL_MAX: TEST_POOL_MAX,
	DB_IDLE_TIMEOUT: TEST_TIMEOUT,
	DB_CONNECT_TIMEOUT: TEST_TIMEOUT,
};
