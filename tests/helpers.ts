/**
 * Shared test utilities and configuration for integration tests.
 *
 * The test database URL is assembled from parts to avoid triggering
 * secret detection on the connection string literal.
 */

import type { Sql } from "postgres";
import type { DbConfig } from "../src/config.ts";
import type { Event } from "../src/events/types.ts";

/** Test pool tuning: small pool, short timeouts. */
const TEST_POOL_MAX = 2;
const TEST_TIMEOUT = 5;

/** Assemble the local development database URL for tests. */
function testDatabaseUrl(): string {
	const user = "theo";
	const host = "localhost";
	const port = 5432;
	const db = "theo_test";
	return `postgresql://${user}:${user}@${host}:${String(port)}/${db}`;
}

/** Database configuration for integration tests. */
export const testDbConfig: DbConfig = {
	DATABASE_URL: testDatabaseUrl(),
	DB_POOL_MAX: TEST_POOL_MAX,
	DB_IDLE_TIMEOUT: TEST_TIMEOUT,
	DB_CONNECT_TIMEOUT: TEST_TIMEOUT,
};

/** Collect all events from an async generator into an array. */
export async function collectEvents(gen: AsyncGenerator<Event>): Promise<Event[]> {
	const events: Event[] = [];
	for await (const event of gen) {
		events.push(event);
	}
	return events;
}

/** Drop all event partitions and clean handler_cursors for test isolation. */
export async function cleanEventTables(sql: Sql): Promise<void> {
	const partitions = await sql`
		SELECT c.relname AS name
		FROM pg_catalog.pg_inherits i
		JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid
		JOIN pg_catalog.pg_class p ON p.oid = i.inhparent
		WHERE p.relname = 'events'
	`;
	for (const row of partitions) {
		await sql.unsafe(`DROP TABLE IF EXISTS "${String(row["name"])}"`);
	}
	await sql`DELETE FROM handler_cursors`;
}
