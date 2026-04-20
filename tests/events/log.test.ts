/**
 * Integration tests for the EventLog.
 *
 * Requires Docker PostgreSQL running via `just up`.
 * Tests cover: append/read roundtrip, upcaster application on read,
 * partition creation, type filtering, and transaction support.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Pool } from "../../src/db/pool.ts";
import type { EventId } from "../../src/events/ids.ts";
import type { EventLog } from "../../src/events/log.ts";
import { createEventLog, partitionBounds, partitionName } from "../../src/events/log.ts";
import type { Event } from "../../src/events/types.ts";
import type { UpcasterRegistry } from "../../src/events/upcasters.ts";
import { createUpcasterRegistry } from "../../src/events/upcasters.ts";
import { cleanEventTables, collectEvents, createTestPool, expectMonotonicIds } from "../helpers.ts";

let pool: Pool;
let log: EventLog;
let upcasters: UpcasterRegistry;

beforeAll(async () => {
	pool = createTestPool();
	const connectResult = await pool.connect();
	if (!connectResult.ok) {
		throw new Error(`Test setup failed: ${connectResult.error.message}`);
	}
	// Schema is set up by `just test-db` before bun test starts.
});

beforeEach(async () => {
	await cleanEventTables(pool.sql);
	upcasters = createUpcasterRegistry();
	log = createEventLog(pool.sql, upcasters);
});

afterAll(async () => {
	if (pool) {
		await pool.end();
	}
});

describe("EventLog", () => {
	test("append returns complete event with ULID id and timestamp", async () => {
		const event = await log.append({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "hello", channel: "cli" },
			metadata: {},
		});

		expect(event.id).toBeDefined();
		expect(typeof event.id).toBe("string");
		expect(event.id.length).toBeGreaterThan(0);
		expect(event.timestamp).toBeInstanceOf(Date);
		expect(event.type).toBe("message.received");
		expect(event.version).toBe(1);
		expect(event.actor).toBe("user");
		expect(event.data).toEqual({ body: "hello", channel: "cli" });
		expect(event.metadata).toEqual({});
	});

	test("read returns events in ULID order", async () => {
		const e1 = await log.append({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "first", channel: "cli" },
			metadata: {},
		});
		const e2 = await log.append({
			type: "turn.started",
			version: 1,
			actor: "theo",
			data: { sessionId: "s1", prompt: "first" },
			metadata: {},
		});
		const e3 = await log.append({
			type: "turn.completed",
			version: 1,
			actor: "theo",
			data: {
				sessionId: "s1",
				responseBody: "hi",
				durationMs: 50,
				inputTokens: 5,
				outputTokens: 5,
				totalTokens: 10,
				costUsd: 0.0001,
			},
			metadata: {},
		});

		const events = await collectEvents(log.read());
		expect(events).toHaveLength(3);
		expect(events[0]?.id).toBe(e1.id);
		expect(events[1]?.id).toBe(e2.id);
		expect(events[2]?.id).toBe(e3.id);

		// Verify ULID ordering: each id is lexicographically greater
		expect(e1.id < e2.id).toBe(true);
		expect(e2.id < e3.id).toBe(true);
	});

	test("readAfter skips past events", async () => {
		const events: Event[] = [];
		for (let i = 0; i < 5; i++) {
			const e = await log.append({
				type: "message.received",
				version: 1,
				actor: "user",
				data: { body: `msg-${String(i)}`, channel: "cli" },
				metadata: {},
			});
			events.push(e);
		}

		const event3Id = events[2]?.id;
		if (event3Id === undefined) throw new Error("Missing event 3");

		const afterEvents = await collectEvents(log.readAfter(event3Id));
		expect(afterEvents).toHaveLength(2);
		expect(afterEvents[0]?.id).toBe(events[3]?.id);
		expect(afterEvents[1]?.id).toBe(events[4]?.id);
	});

	test("upcasters applied on read", async () => {
		// Register an upcaster: v1 -> v2 adds a newField
		upcasters.register("message.received", 1, (data) => ({
			...data,
			newField: "added-in-v2",
		}));

		// Re-create log with the updated upcasters
		log = createEventLog(pool.sql, upcasters);

		// Append a v1 event (still stored as v1 in the database)
		await log.append({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "original", channel: "cli" },
			metadata: {},
		});

		// Read back - should have upcaster applied
		const events = await collectEvents(log.read());
		expect(events).toHaveLength(1);
		const event = events[0];
		expect(event?.version).toBe(2);
		// Upcasted data has fields beyond the static type — use Record for dynamic access
		const data: Record<string, unknown> = event?.data as unknown as Record<string, unknown>;
		expect(data["newField"]).toBe("added-in-v2");
		expect(data["body"]).toBe("original");
	});

	test("type filter returns only matching events", async () => {
		await log.append({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "hello", channel: "cli" },
			metadata: {},
		});
		await log.append({
			type: "turn.started",
			version: 1,
			actor: "theo",
			data: { sessionId: "s1" },
			metadata: {},
		});
		await log.append({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "world", channel: "cli" },
			metadata: {},
		});

		const filtered = await collectEvents(log.read({ types: ["message.received"] }));
		expect(filtered).toHaveLength(2);
		expect(filtered[0]?.type).toBe("message.received");
		expect(filtered[1]?.type).toBe("message.received");
	});

	test("partition auto-created on append", async () => {
		// Append an event (this triggers partition creation for the current month)
		await log.append({
			type: "system.started",
			version: 1,
			actor: "system",
			data: { version: "0.1.0" },
			metadata: {},
		});

		// Verify the partition exists in pg_catalog
		const now = new Date();
		const expectedName = partitionName(now);
		const rows = await pool.sql`
			SELECT c.relname AS name
			FROM pg_catalog.pg_inherits i
			JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid
			JOIN pg_catalog.pg_class p ON p.oid = i.inhparent
			WHERE p.relname = 'events' AND c.relname = ${expectedName}
		`;
		expect(rows).toHaveLength(1);

		// Verify the event is readable
		const events = await collectEvents(log.read());
		expect(events).toHaveLength(1);
	});

	test("append with tx uses the provided transaction", async () => {
		// Append inside a transaction, then verify it's visible after commit
		let eventId: EventId | undefined;

		await pool.sql.begin(async (tx) => {
			const event = await log.append(
				{
					type: "system.started",
					version: 1,
					actor: "system",
					data: { version: "0.1.0" },
					metadata: {},
				},
				tx,
			);
			eventId = event.id;
		});

		// After commit, verify the event is visible
		const events = await collectEvents(log.read());
		expect(events).toHaveLength(1);
		expect(events[0]?.id).toBe(eventId);
	});

	test("loadKnownPartitions populates the set", async () => {
		// Create a partition manually
		const now = new Date();
		await log.ensurePartition(now);

		// Create a fresh log and load partitions
		const freshLog = createEventLog(pool.sql, upcasters);
		await freshLog.loadKnownPartitions();

		// The ensurePartition call on the fresh log should be a no-op (partition already known)
		// We verify indirectly by appending (which calls ensurePartition)
		const event = await freshLog.append({
			type: "system.started",
			version: 1,
			actor: "system",
			data: { version: "0.1.0" },
			metadata: {},
		});

		expect(event.id).toBeDefined();
	});

	test("read on empty table yields nothing", async () => {
		await log.ensurePartition(new Date());
		const events = await collectEvents(log.read());
		expect(events).toHaveLength(0);
	});

	test("readAfter with nonexistent cursor returns events after that ULID", async () => {
		const e1 = await log.append({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "first", channel: "cli" },
			metadata: {},
		});
		// Ensure e2 gets a different millisecond timestamp so there's ULID space between them
		await Bun.sleep(2);
		const e2 = await log.append({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "second", channel: "cli" },
			metadata: {},
		});

		// Fabricate a cursor between e1 and e2. Same-ms monotonic ULIDs are adjacent
		// (increment by 1), so we force different timestamps with a small sleep.
		// Then: e1's 10-char timestamp prefix + max random (all Z's) is guaranteed
		// > e1 (same ts, higher random) and < e2 (lower timestamp prefix).
		const fabricated = `${e1.id.slice(0, 10)}ZZZZZZZZZZZZZZZZ` as EventId;
		expect(fabricated > e1.id).toBe(true);
		expect(fabricated < e2.id).toBe(true);

		const events = await collectEvents(log.readAfter(fabricated));
		expect(events).toHaveLength(1);
		expect(events[0]?.id).toBe(e2.id);
	});

	test("append with rolled-back transaction is invisible", async () => {
		await log.ensurePartition(new Date());

		try {
			await pool.sql.begin(async (tx) => {
				await log.append(
					{
						type: "system.started",
						version: 1,
						actor: "system",
						data: { version: "0.1.0" },
						metadata: {},
					},
					tx,
				);
				throw new Error("Force rollback");
			});
		} catch {
			// Expected — the transaction was rolled back
		}

		const events = await collectEvents(log.read());
		expect(events).toHaveLength(0);
	});

	test("cursor streaming works across batch boundaries", async () => {
		// Insert more events than the cursor batch size (100), batched in a
		// single transaction to avoid 150 sequential round-trips.
		const count = 150;
		await pool.sql.begin(async (tx) => {
			for (let i = 0; i < count; i++) {
				await log.append(
					{
						type: "message.received",
						version: 1,
						actor: "user",
						data: { body: `msg-${String(i)}`, channel: "cli" },
						metadata: {},
					},
					tx,
				);
			}
		});

		const events = await collectEvents(log.read());
		expect(events).toHaveLength(count);

		// Verify ULID ordering across batch boundary
		expectMonotonicIds(events.map((e) => e.id));
	});
});

describe("Partition helpers", () => {
	test("partitionName computes correct format", () => {
		const jan2026 = new Date(Date.UTC(2026, 0, 15));
		expect(partitionName(jan2026)).toBe("events_2026_01");

		const dec2025 = new Date(Date.UTC(2025, 11, 1));
		expect(partitionName(dec2025)).toBe("events_2025_12");
	});

	test("partitionBounds computes correct [from, to) range", () => {
		const march2026 = new Date(Date.UTC(2026, 2, 15));
		const bounds = partitionBounds(march2026);
		expect(bounds.from.toISOString()).toBe("2026-03-01T00:00:00.000Z");
		expect(bounds.to.toISOString()).toBe("2026-04-01T00:00:00.000Z");
	});

	test("partitionBounds handles December to January rollover", () => {
		const dec2025 = new Date(Date.UTC(2025, 11, 25));
		const bounds = partitionBounds(dec2025);
		expect(bounds.from.toISOString()).toBe("2025-12-01T00:00:00.000Z");
		expect(bounds.to.toISOString()).toBe("2026-01-01T00:00:00.000Z");
	});
});
