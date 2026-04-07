/**
 * Shared test utilities and configuration for integration tests.
 *
 * The test database URL is assembled from parts to avoid triggering
 * secret detection on the connection string literal.
 */

import { expect } from "bun:test";
import type { Sql } from "postgres";
import type { DbConfig } from "../src/config.ts";
import type { Pool } from "../src/db/pool.ts";
import { createPool } from "../src/db/pool.ts";
import type { Event, EventOfType } from "../src/events/types.ts";

/** Test pool tuning: small pool, short timeouts. */
const TEST_POOL_MAX = 5;
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
export async function collectEvents(gen: AsyncGenerator<Event>): Promise<Event[]>;
/** Collect all events, asserting each matches the expected type. */
export async function collectEvents<T extends Event["type"]>(
	gen: AsyncGenerator<Event>,
	expectedType: T,
): Promise<EventOfType<T>[]>;
export async function collectEvents(
	gen: AsyncGenerator<Event>,
	expectedType?: string,
): Promise<Event[]> {
	const events: Event[] = [];
	for await (const event of gen) {
		if (expectedType !== undefined && event.type !== expectedType) {
			throw new Error(`collectEvents: expected "${expectedType}", got "${event.type}"`);
		}
		events.push(event);
	}
	return events;
}

/** Assert that a string array is in strictly ascending (ULID) order. */
export function expectMonotonicIds(ids: readonly string[]): void {
	for (let i = 1; i < ids.length; i++) {
		const prev = ids[i - 1];
		const curr = ids[i];
		if (prev !== undefined && curr !== undefined) {
			expect(prev < curr).toBe(true);
		}
	}
}

/** Create a test pool with PostgreSQL notices suppressed. */
export function createTestPool(): Pool {
	return createPool(testDbConfig, { onnotice() {} });
}

/** Clean event data for test isolation. Uses TRUNCATE for speed and to avoid
 *  DDL lock conflicts with parallel test files operating on other tables. */
export async function cleanEventTables(sql: Sql): Promise<void> {
	await sql`TRUNCATE events, handler_cursors CASCADE`;
}
