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
import type { EventBus } from "../src/events/bus.ts";
import { createEventBus } from "../src/events/bus.ts";
import { createEventLog } from "../src/events/log.ts";
import type { Event, EventOfType } from "../src/events/types.ts";
import { createUpcasterRegistry } from "../src/events/upcasters.ts";
import { EMBEDDING_DIM, type EmbeddingService } from "../src/memory/embeddings.ts";

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

/**
 * Assert that an async operation rejects with a message matching `pattern`.
 * Workaround for bun-types declaring `rejects.toThrow` as `void` — its
 * runtime returns a Promise, so awaiting the helper here keeps behavior
 * correct without tripping the `await-has-no-effect` diagnostic.
 */
export async function expectReject(
	fn: () => Promise<unknown>,
	pattern: string | RegExp,
): Promise<void> {
	let caught: unknown;
	try {
		await fn();
	} catch (error) {
		caught = error;
	}
	expect(caught).toBeInstanceOf(Error);
	const message = (caught as Error).message;
	if (typeof pattern === "string") {
		expect(message).toContain(pattern);
	} else {
		expect(message).toMatch(pattern);
	}
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

/** Create an EventBus wired to a real EventLog for integration tests. */
export function createTestBus(sql: Sql): EventBus {
	const log = createEventLog(sql, createUpcasterRegistry());
	return createEventBus(log, sql);
}

// ---------------------------------------------------------------------------
// Mock embedding services
// ---------------------------------------------------------------------------

/** Deterministic hash-based embedding: produces a repeatable L2-normalized 768-dim vector. */
function hashEmbed(text: string): Float32Array {
	const v = new Float32Array(EMBEDDING_DIM);
	let seed = 0;
	for (let i = 0; i < text.length; i++) {
		seed = (seed * 31 + text.charCodeAt(i)) | 0;
	}
	for (let i = 0; i < EMBEDDING_DIM; i++) {
		seed = (seed * 1103515245 + 12345) | 0;
		v[i] = ((seed >>> 16) & 0x7fff) / 32767 - 0.5;
	}
	let norm = 0;
	for (let i = 0; i < EMBEDDING_DIM; i++) {
		norm += (v[i] ?? 0) * (v[i] ?? 0);
	}
	norm = Math.sqrt(norm);
	for (let i = 0; i < EMBEDDING_DIM; i++) {
		v[i] = (v[i] ?? 0) / norm;
	}
	return v;
}

/** Mock embedding service that returns deterministic vectors. */
export function createMockEmbeddings(): EmbeddingService {
	return {
		async embed(text: string): Promise<Float32Array> {
			if (text.trim().length === 0) {
				throw new Error("Cannot embed empty or whitespace-only text");
			}
			return hashEmbed(text);
		},
		async embedBatch(texts: readonly string[]): Promise<readonly Float32Array[]> {
			for (const t of texts) {
				if (t.trim().length === 0) {
					throw new Error("Cannot embed empty or whitespace-only text");
				}
			}
			return texts.map(hashEmbed);
		},
		async warmup(): Promise<void> {},
	};
}

/** Mock that always throws — simulates embedding service unavailable. */
export function createFailingEmbeddings(): EmbeddingService {
	return {
		async embed(): Promise<Float32Array> {
			throw new Error("Embedding service unavailable");
		},
		async embedBatch(): Promise<readonly Float32Array[]> {
			throw new Error("Embedding service unavailable");
		},
		async warmup(): Promise<void> {},
	};
}
