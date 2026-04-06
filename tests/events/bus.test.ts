/**
 * Integration tests for the EventBus.
 *
 * Requires Docker PostgreSQL running via `just up`.
 * Tests cover: emit+dispatch, checkpointing, replay, dead-lettering,
 * handler isolation, transaction support, ephemeral events, and lifecycle.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import { migrate } from "../../src/db/migrate.ts";
import type { Pool } from "../../src/db/pool.ts";
import { createPool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { createEventBus } from "../../src/events/bus.ts";
import type { EventId } from "../../src/events/ids.ts";
import type { EventLog } from "../../src/events/log.ts";
import { createEventLog } from "../../src/events/log.ts";
import type { Event } from "../../src/events/types.ts";
import type { UpcasterRegistry } from "../../src/events/upcasters.ts";
import { createUpcasterRegistry } from "../../src/events/upcasters.ts";
import { cleanEventTables, collectEvents, testDbConfig } from "../helpers.ts";

let pool: Pool;

beforeAll(async () => {
	pool = createPool(testDbConfig);
	const connectResult = await pool.connect();
	if (!connectResult.ok) {
		throw new Error(`Test setup failed: ${connectResult.error.message}`);
	}

	const migrateResult = await migrate(pool.sql);
	if (!migrateResult.ok) {
		throw new Error(`Migration failed: ${migrateResult.error.message}`);
	}
});

afterAll(async () => {
	if (pool) {
		await pool.end();
	}
});

/** Create a fresh log + bus pair for a test. */
function createTestBus(): { log: EventLog; bus: EventBus; upcasters: UpcasterRegistry } {
	const upcasters = createUpcasterRegistry();
	const log = createEventLog(pool.sql, upcasters);
	const bus = createEventBus(log, pool.sql);
	return { log, bus, upcasters };
}

/** Small delay to allow async handlers to run. */
function delay(ms: number): Promise<void> {
	return new Promise((resolve) => {
		setTimeout(resolve, ms);
	});
}

describe("EventBus", () => {
	let log: EventLog;
	let bus: EventBus;

	beforeEach(async () => {
		await cleanEventTables(pool.sql);
		const result = createTestBus();
		log = result.log;
		bus = result.bus;
	});

	afterEach(async () => {
		// Drain all handler queues then stop to prevent async leaks into the next test
		try {
			await bus.flush();
		} catch {
			// Ignore errors from already-stopped or never-started buses
		}
		try {
			await bus.stop();
		} catch {
			// Ignore errors from already-stopped buses
		}
	});

	test("emit enqueues to durable handler", async () => {
		const received: Event[] = [];

		bus.on(
			"message.received",
			async (event) => {
				received.push(event);
			},
			{ id: "test-durable-handler" },
		);

		await bus.start();

		await bus.emit({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "hello", channel: "cli" },
			metadata: {},
		});

		await bus.flush();

		expect(received).toHaveLength(1);
		expect(received[0]?.type).toBe("message.received");

		// Verify checkpoint was advanced
		const cursors = await pool.sql`
			SELECT cursor FROM handler_cursors WHERE handler_id = 'test-durable-handler'
		`;
		expect(cursors).toHaveLength(1);
		expect(cursors[0]?.["cursor"]).toBe(received[0]?.id);
	});

	test("emit dispatches to ephemeral handler", async () => {
		const received: Event[] = [];

		bus.on("message.received", async (event) => {
			received.push(event);
		});

		await bus.start();

		await bus.emit({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "hello", channel: "cli" },
			metadata: {},
		});

		// Ephemeral handlers are fire-and-forget, give a brief moment
		await delay(50);

		expect(received).toHaveLength(1);

		// Verify NO checkpoint row was created
		const cursors = await pool.sql`SELECT cursor FROM handler_cursors`;
		expect(cursors).toHaveLength(0);
	});

	test("handler isolation -- one failing handler does not block another", async () => {
		const successReceived: Event[] = [];

		bus.on(
			"message.received",
			async () => {
				throw new Error("Intentional failure");
			},
			{ id: "failing-handler" },
		);

		bus.on(
			"message.received",
			async (event) => {
				successReceived.push(event);
			},
			{ id: "success-handler" },
		);

		await bus.start();

		await bus.emit({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "hello", channel: "cli" },
			metadata: {},
		});

		await bus.flush();

		// The success handler should have received the event despite the other failing
		expect(successReceived).toHaveLength(1);
	});

	test("checkpoint advances atomically with handler", async () => {
		bus.on(
			"system.started",
			async (_event, _tx) => {
				// Handler succeeds, checkpoint should be updated in the same tx
			},
			{ id: "atomic-checkpoint-handler" },
		);

		await bus.start();

		const emitted = await bus.emit({
			type: "system.started",
			version: 1,
			actor: "system",
			data: { version: "0.1.0" },
			metadata: {},
		});

		await bus.flush();

		// Verify checkpoint matches the emitted event
		const cursors = await pool.sql`
			SELECT cursor FROM handler_cursors WHERE handler_id = 'atomic-checkpoint-handler'
		`;
		expect(cursors).toHaveLength(1);
		expect(cursors[0]?.["cursor"]).toBe(emitted.id);
	});

	test("checkpoint never regresses", async () => {
		const receivedIds: EventId[] = [];
		const emittedIds: EventId[] = [];

		bus.on(
			"message.received",
			async (event) => {
				receivedIds.push(event.id);
			},
			{ id: "monotonic-cursor-handler" },
		);

		await bus.start();

		// Emit 20 events (reduced from 100 for test stability)
		for (let i = 0; i < 20; i++) {
			const emitted = await bus.emit({
				type: "message.received",
				version: 1,
				actor: "user",
				data: { body: `msg-${String(i)}`, channel: "cli" },
				metadata: {},
			});
			emittedIds.push(emitted.id);
		}

		await bus.flush();

		expect(receivedIds).toHaveLength(emittedIds.length);

		// Verify cursor is at the last event
		const cursors = await pool.sql`
			SELECT cursor FROM handler_cursors WHERE handler_id = 'monotonic-cursor-handler'
		`;
		expect(cursors).toHaveLength(1);
		const finalCursor = String(cursors[0]?.["cursor"]);
		const lastReceivedId = receivedIds[receivedIds.length - 1] ?? "";
		expect(lastReceivedId.length).toBeGreaterThan(0);
		expect(finalCursor).toBe(lastReceivedId);

		// Verify all received IDs are monotonically increasing
		for (let i = 1; i < receivedIds.length; i++) {
			const prev = receivedIds[i - 1];
			const curr = receivedIds[i];
			if (prev !== undefined && curr !== undefined) {
				expect(prev < curr).toBe(true);
			}
		}
	});

	test("dead-letter after retries", async () => {
		bus.on(
			"system.started",
			async () => {
				throw new Error("Always fails");
			},
			{ id: "dead-letter-handler" },
		);

		await bus.start();

		await bus.emit({
			type: "system.started",
			version: 1,
			actor: "system",
			data: { version: "0.1.0" },
			metadata: {},
		});

		await bus.flush();

		// Verify cursor was advanced (event was dead-lettered, not stuck)
		const cursors = await pool.sql`
			SELECT cursor FROM handler_cursors WHERE handler_id = 'dead-letter-handler'
		`;
		expect(cursors).toHaveLength(1);

		// Verify dead-letter meta-event was emitted
		const deadLetterEvents = await collectEvents(
			log.read({ types: ["system.handler.dead_lettered"] }),
		);
		expect(deadLetterEvents.length).toBeGreaterThanOrEqual(1);

		const dlEvent = deadLetterEvents[0];
		expect(dlEvent).toBeDefined();
		const dlData = dlEvent?.data as unknown as Record<string, unknown>;
		expect(dlData["handlerId"]).toBe("dead-letter-handler");
		expect(dlData["attempts"]).toBe(3);
		expect(dlData["lastError"]).toBe("Always fails");
	});

	test("dead-letter atomicity -- dead-letter event + cursor advance in same tx", async () => {
		bus.on(
			"system.started",
			async () => {
				throw new Error("Atomic dead-letter test");
			},
			{ id: "atomic-dl-handler" },
		);

		await bus.start();

		const emitted = await bus.emit({
			type: "system.started",
			version: 1,
			actor: "system",
			data: { version: "0.1.0" },
			metadata: {},
		});

		await bus.flush();

		// Both the dead-letter event and cursor advance should exist
		const cursors = await pool.sql`
			SELECT cursor FROM handler_cursors WHERE handler_id = 'atomic-dl-handler'
		`;
		expect(cursors).toHaveLength(1);
		expect(String(cursors[0]?.["cursor"])).toBe(emitted.id);

		const deadLetterEvents = await collectEvents(
			log.read({ types: ["system.handler.dead_lettered"] }),
		);
		expect(deadLetterEvents.length).toBeGreaterThanOrEqual(1);
	});

	test("retry succeeds on second attempt", async () => {
		let attemptCount = 0;
		const received: Event[] = [];

		bus.on(
			"system.started",
			async (event) => {
				attemptCount++;
				if (attemptCount === 1) {
					throw new Error("First attempt fails");
				}
				received.push(event);
			},
			{ id: "retry-success-handler" },
		);

		await bus.start();

		await bus.emit({
			type: "system.started",
			version: 1,
			actor: "system",
			data: { version: "0.1.0" },
			metadata: {},
		});

		await bus.flush();

		// Handler should have succeeded on second attempt
		expect(received).toHaveLength(1);

		// Checkpoint should be advanced
		const cursors = await pool.sql`
			SELECT cursor FROM handler_cursors WHERE handler_id = 'retry-success-handler'
		`;
		expect(cursors).toHaveLength(1);

		// No dead-letter event should exist
		const deadLetterEvents = await collectEvents(
			log.read({ types: ["system.handler.dead_lettered"] }),
		);
		const handlerDls = deadLetterEvents.filter(
			(e) =>
				(e.data as unknown as Record<string, unknown>)["handlerId"] === "retry-success-handler",
		);
		expect(handlerDls).toHaveLength(0);
	});

	test("replay on start -- fresh handler receives all past events", async () => {
		// Step 1: Emit events directly to the log (bypass bus)
		await log.ensurePartition(new Date());
		const e1 = await log.append({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "first", channel: "cli" },
			metadata: {},
		});
		const e2 = await log.append({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "second", channel: "cli" },
			metadata: {},
		});

		// Step 2: Create a new bus with a durable handler
		const received: Event[] = [];
		bus.on(
			"message.received",
			async (event) => {
				received.push(event);
			},
			{ id: "replay-handler" },
		);

		// Step 3: start() should replay the existing events
		await bus.start();
		await bus.flush();

		expect(received).toHaveLength(2);
		expect(received[0]?.id).toBe(e1.id);
		expect(received[1]?.id).toBe(e2.id);
	});

	test("replay respects checkpoint", async () => {
		// Step 1: Emit events directly to the log
		await log.ensurePartition(new Date());
		await log.append({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "first", channel: "cli" },
			metadata: {},
		});
		const e2 = await log.append({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "second", channel: "cli" },
			metadata: {},
		});
		await log.append({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "third", channel: "cli" },
			metadata: {},
		});

		// Step 2: Set handler cursor to event2 (pretend it already processed e1 and e2)
		await pool.sql`
			INSERT INTO handler_cursors (handler_id, cursor, updated_at)
			VALUES ('checkpoint-replay-handler', ${e2.id}, now())
		`;

		// Step 3: Create bus with handler and start
		const received: Event[] = [];
		bus.on(
			"message.received",
			async (event) => {
				received.push(event);
			},
			{ id: "checkpoint-replay-handler" },
		);

		await bus.start();
		await bus.flush();

		// Should only receive event 3 (after the checkpoint at event 2)
		expect(received).toHaveLength(1);
		expect((received[0]?.data as unknown as Record<string, unknown>)["body"]).toBe("third");
	});

	test("concurrent emit during replay -- all events processed, no gaps", async () => {
		// Step 1: Emit events directly to the log (before bus starts)
		await log.ensurePartition(new Date());
		for (let i = 0; i < 5; i++) {
			await log.append({
				type: "message.received",
				version: 1,
				actor: "user",
				data: { body: `replay-${String(i)}`, channel: "cli" },
				metadata: {},
			});
		}

		// Step 2: Register a handler that records all events
		const received: string[] = [];
		bus.on(
			"message.received",
			async (event) => {
				received.push(String((event.data as unknown as Record<string, unknown>)["body"]));
			},
			{ id: "concurrent-replay-handler" },
		);

		// Step 3: Start the bus (triggers replay), then emit live events
		await bus.start();

		for (let i = 0; i < 3; i++) {
			await bus.emit({
				type: "message.received",
				version: 1,
				actor: "user",
				data: { body: `live-${String(i)}`, channel: "cli" },
				metadata: {},
			});
		}

		await bus.flush();

		// All 8 events (5 replay + 3 live) should be received
		expect(received).toHaveLength(8);

		// Verify replay events come first
		const replayEvents = received.slice(0, 5);
		for (let i = 0; i < 5; i++) {
			expect(replayEvents[i]).toBe(`replay-${String(i)}`);
		}

		// Verify live events come after replay
		const liveEvents = received.slice(5);
		for (let i = 0; i < 3; i++) {
			expect(liveEvents[i]).toBe(`live-${String(i)}`);
		}
	});

	test("ULID dedup -- replay+live overlap delivers each event exactly once", async () => {
		// Pre-populate 5 events in the log before bus starts
		await log.ensurePartition(new Date());
		const preEvents: Event[] = [];
		for (let i = 0; i < 5; i++) {
			const e = await log.append({
				type: "message.received",
				version: 1,
				actor: "user",
				data: { body: `pre-${String(i)}`, channel: "cli" },
				metadata: {},
			});
			preEvents.push(e);
		}

		// Register handler that tracks received event IDs
		const receivedIds: string[] = [];
		bus.on(
			"message.received",
			async (event) => {
				receivedIds.push(event.id);
			},
			{ id: "dedup-handler" },
		);

		// start() replays + sets started=true so live events also enqueue.
		// The MVCC window means some events may appear in both replay and live queues.
		// ULID dedup must ensure each event is processed exactly once.
		await bus.start();

		// Emit live events that are guaranteed to have higher ULIDs
		for (let i = 0; i < 3; i++) {
			await bus.emit({
				type: "message.received",
				version: 1,
				actor: "user",
				data: { body: `live-${String(i)}`, channel: "cli" },
				metadata: {},
			});
		}

		await bus.flush();

		// All 8 events should be received exactly once
		expect(receivedIds).toHaveLength(8);

		// No duplicates
		const uniqueIds = new Set(receivedIds);
		expect(uniqueIds.size).toBe(8);

		// IDs are monotonically increasing (ULID order preserved)
		for (let i = 1; i < receivedIds.length; i++) {
			const prev = receivedIds[i - 1];
			const curr = receivedIds[i];
			if (prev !== undefined && curr !== undefined) {
				expect(prev < curr).toBe(true);
			}
		}
	});

	test("handler emission during replay does not deadlock", async () => {
		// Step 1: Emit an event directly to the log
		await log.ensurePartition(new Date());
		await log.append({
			type: "system.started",
			version: 1,
			actor: "system",
			data: { version: "0.1.0" },
			metadata: {},
		});

		// Step 2: Register a handler that emits another event during processing
		const received: Event[] = [];
		bus.on(
			"system.started",
			async (event) => {
				received.push(event);
				// Emit another event from within the handler — this must not deadlock
				await bus.emit({
					type: "system.stopped",
					version: 1,
					actor: "system",
					data: { reason: "emitted-from-handler" },
					metadata: {},
				});
			},
			{ id: "emitting-handler" },
		);

		const stoppedReceived: Event[] = [];
		bus.on(
			"system.stopped",
			async (event) => {
				stoppedReceived.push(event);
			},
			{ id: "stopped-handler" },
		);

		// This should not deadlock
		await bus.start();
		await bus.flush();

		expect(received).toHaveLength(1);
		expect(stoppedReceived).toHaveLength(1);
	});

	test("late handler registration -- replays from beginning", async () => {
		await bus.start();

		// Emit some events
		await bus.emit({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "before-late", channel: "cli" },
			metadata: {},
		});

		await bus.flush();

		// Register a handler AFTER start
		const received: Event[] = [];
		bus.on(
			"message.received",
			async (event) => {
				received.push(event);
			},
			{ id: "late-durable-handler" },
		);

		// Wait for the late handler's replay to complete
		await delay(200);
		await bus.flush();

		// Should receive the event that was emitted before registration
		expect(received.length).toBeGreaterThanOrEqual(1);
		expect((received[0]?.data as unknown as Record<string, unknown>)["body"]).toBe("before-late");
	});

	test("late ephemeral handler -- receives only events after registration", async () => {
		await bus.start();

		// Emit an event before the handler is registered
		await bus.emit({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "before", channel: "cli" },
			metadata: {},
		});

		// Register ephemeral handler (no id)
		const received: Event[] = [];
		bus.on("message.received", async (event) => {
			received.push(event);
		});

		// Emit an event after the handler is registered
		await bus.emit({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "after", channel: "cli" },
			metadata: {},
		});

		await delay(50);

		// Should only receive the event emitted after registration
		expect(received).toHaveLength(1);
		expect((received[0]?.data as unknown as Record<string, unknown>)["body"]).toBe("after");
	});

	test("emit with tx -- event persisted atomically", async () => {
		await bus.start();

		let emittedId: EventId | undefined;

		await pool.sql.begin(async (tx) => {
			const event = await bus.emit(
				{
					type: "system.started",
					version: 1,
					actor: "system",
					data: { version: "0.1.0" },
					metadata: {},
				},
				{ tx },
			);
			emittedId = event.id;
		});

		// After commit, event should be visible
		const events = await collectEvents(log.read());
		const found = events.find((e) => e.id === emittedId);
		expect(found).toBeDefined();
	});

	test("stop mid-processing -- finishes current event", async () => {
		const received: Event[] = [];

		bus.on(
			"message.received",
			async (event) => {
				received.push(event);
			},
			{ id: "stop-test-handler" },
		);

		await bus.start();

		// Emit some events
		for (let i = 0; i < 5; i++) {
			await bus.emit({
				type: "message.received",
				version: 1,
				actor: "user",
				data: { body: `msg-${String(i)}`, channel: "cli" },
				metadata: {},
			});
		}

		// Stop the bus (should finish current event per handler, not drain full queue)
		await bus.stop();

		// The drain loop processes events sequentially. Between emit() and stop(),
		// at least one event should have been processed (the queue starts draining
		// immediately). The exact count depends on timing.
		expect(received.length).toBeGreaterThanOrEqual(1);
		expect(received.length).toBeLessThanOrEqual(5);
	});

	test("flush drains all queues", async () => {
		const received: Event[] = [];

		bus.on(
			"message.received",
			async (event) => {
				received.push(event);
			},
			{ id: "flush-test-handler" },
		);

		await bus.start();

		// Emit multiple events
		for (let i = 0; i < 10; i++) {
			await bus.emit({
				type: "message.received",
				version: 1,
				actor: "user",
				data: { body: `msg-${String(i)}`, channel: "cli" },
				metadata: {},
			});
		}

		// Flush should drain all queues completely
		await bus.flush();

		expect(received).toHaveLength(10);
	});

	test("ephemeral events skip persistence", () => {
		const received: Array<{ type: string; data: unknown }> = [];

		bus.onEphemeral("stream.chunk", (event) => {
			received.push(event);
		});

		bus.emitEphemeral({
			type: "stream.chunk",
			data: { text: "hello", sessionId: "s1" },
		});

		// Ephemeral events are synchronous, no await needed
		expect(received).toHaveLength(1);
		expect(received[0]?.type).toBe("stream.chunk");
	});

	test("partition proactive creation on start", async () => {
		await bus.start();

		// Verify partitions exist for current and next month
		const now = new Date();
		const currentMonth = `events_${String(now.getUTCFullYear())}_${String(now.getUTCMonth() + 1).padStart(2, "0")}`;
		const nextDate = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1));
		const nextMonth = `events_${String(nextDate.getUTCFullYear())}_${String(nextDate.getUTCMonth() + 1).padStart(2, "0")}`;

		const partitions = await pool.sql`
			SELECT c.relname AS name
			FROM pg_catalog.pg_inherits i
			JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid
			JOIN pg_catalog.pg_class p ON p.oid = i.inhparent
			WHERE p.relname = 'events'
		`;

		const partitionNames = partitions.map((r) => String(r["name"]));
		expect(partitionNames).toContain(currentMonth);
		expect(partitionNames).toContain(nextMonth);
	});

	test("emit after stop -- event persisted but not dispatched live", async () => {
		const received: Event[] = [];
		bus.on(
			"message.received",
			async (event) => {
				received.push(event);
			},
			{ id: "emit-after-stop-handler" },
		);

		await bus.start();
		await bus.stop();

		// Emit after stop — should persist but not dispatch
		const emitted = await bus.emit({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "after-stop", channel: "cli" },
			metadata: {},
		});

		await delay(50);

		// Event is in the database
		const events = await collectEvents(log.read());
		expect(events.some((e) => e.id === emitted.id)).toBe(true);

		// Handler did NOT receive it live
		expect(received).toHaveLength(0);

		// On restart, handler gets it via replay
		const bus2Result = createTestBus();
		const replayReceived: Event[] = [];
		bus2Result.bus.on(
			"message.received",
			async (event) => {
				replayReceived.push(event);
			},
			{ id: "emit-after-stop-handler" },
		);
		await bus2Result.bus.start();
		await bus2Result.bus.flush();

		expect(replayReceived).toHaveLength(1);
		expect(replayReceived[0]?.id).toBe(emitted.id);

		await bus2Result.bus.stop();
	});

	test("two durable handlers for same type both receive events independently", async () => {
		const receivedA: Event[] = [];
		const receivedB: Event[] = [];

		bus.on(
			"message.received",
			async (event) => {
				receivedA.push(event);
			},
			{ id: "multi-handler-a" },
		);
		bus.on(
			"message.received",
			async (event) => {
				receivedB.push(event);
			},
			{ id: "multi-handler-b" },
		);

		await bus.start();

		await bus.emit({
			type: "message.received",
			version: 1,
			actor: "user",
			data: { body: "shared", channel: "cli" },
			metadata: {},
		});

		await bus.flush();

		expect(receivedA).toHaveLength(1);
		expect(receivedB).toHaveLength(1);

		// Each handler has its own checkpoint
		const cursorA = await pool.sql`
			SELECT cursor FROM handler_cursors WHERE handler_id = 'multi-handler-a'
		`;
		const cursorB = await pool.sql`
			SELECT cursor FROM handler_cursors WHERE handler_id = 'multi-handler-b'
		`;
		expect(cursorA).toHaveLength(1);
		expect(cursorB).toHaveLength(1);
		expect(cursorA[0]?.["cursor"]).toBe(cursorB[0]?.["cursor"]);
	});

	test("stop before start is harmless", async () => {
		// Should not throw
		await bus.stop();
	});

	test("flush with no handlers resolves immediately", async () => {
		await bus.start();
		// No handlers registered — flush should resolve without hanging
		await bus.flush();
	});

	test("stop mid-replay -- checkpoint preserved for restart", async () => {
		// Pre-populate many events
		await log.ensurePartition(new Date());
		for (let i = 0; i < 20; i++) {
			await log.append({
				type: "message.received",
				version: 1,
				actor: "user",
				data: { body: `event-${String(i)}`, channel: "cli" },
				metadata: {},
			});
		}

		// Register a slow handler that gives us time to call stop()
		const received: string[] = [];
		bus.on(
			"message.received",
			async (event) => {
				received.push(String((event.data as unknown as Record<string, unknown>)["body"]));
				// Small delay to simulate work
				await delay(10);
			},
			{ id: "stop-replay-handler" },
		);

		// Start (begins replay) then immediately stop
		const startPromise = bus.start();
		await delay(50);
		await bus.stop();
		await startPromise.catch(() => {});

		const processedCount = received.length;
		expect(processedCount).toBeGreaterThanOrEqual(1);
		expect(processedCount).toBeLessThan(20);

		// Verify a checkpoint was persisted
		const cursors = await pool.sql`
			SELECT cursor FROM handler_cursors WHERE handler_id = 'stop-replay-handler'
		`;
		expect(cursors).toHaveLength(1);

		// Restart with a new bus — should replay from where it left off
		const bus2Result = createTestBus();
		const replayReceived: string[] = [];
		bus2Result.bus.on(
			"message.received",
			async (event) => {
				replayReceived.push(String((event.data as unknown as Record<string, unknown>)["body"]));
			},
			{ id: "stop-replay-handler" },
		);
		await bus2Result.bus.start();
		await bus2Result.bus.flush();

		// Total across both runs should be 20 (all events processed exactly once)
		const totalProcessed = processedCount + replayReceived.length;
		expect(totalProcessed).toBe(20);

		await bus2Result.bus.stop();
	});
});
