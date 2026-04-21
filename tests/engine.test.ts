/**
 * Engine lifecycle tests.
 *
 * The Engine orchestrates migrations, event bus, scheduler, chat engine, and
 * gate. The tests drive it through its state machine with stub subsystems —
 * real migrations/DB are exercised by integration tests in `tests/db`.
 *
 * Every stub records the call sequence so we can assert:
 *   - startup order: migrate → bus.start → scheduler.start → system.started → gate.start
 *   - shutdown order: gate.stop → scheduler.stop → system.stopped → bus.stop → pool.end
 *   - stopping flag squashes double-stop from signal races
 *   - pause parks messages; resume drains them in order
 */

import { describe, expect, mock, test } from "bun:test";
import type { ChatEngine } from "../src/chat/engine.ts";
import type { TurnResult } from "../src/chat/types.ts";
import type { Pool } from "../src/db/pool.ts";
import { Engine, installSignalHandlers } from "../src/engine.ts";
import type { EventBus } from "../src/events/bus.ts";
import type { Event } from "../src/events/types.ts";
import type { Gate } from "../src/gates/types.ts";
import type { Scheduler } from "../src/scheduler/runner.ts";

// ---------------------------------------------------------------------------
// Stubs
// ---------------------------------------------------------------------------

interface Recorder {
	calls: string[];
}

function createStubPool(recorder: Recorder): Pool {
	// The migrate() call reads a marker from the pool's sql tagged template.
	// We patch the migration runner via dependency — or, simpler, install a
	// pool whose sql throws to short-circuit. But the Engine runs migrate()
	// unconditionally. Instead, route all sql calls through a trivial stub
	// that mocks the migrate runner's behaviour.
	const sql = Object.assign(
		async (): Promise<unknown[]> => {
			return [];
		},
		{
			begin: async (fn: (tx: unknown) => Promise<unknown>): Promise<unknown> => fn({}),
			end: async (): Promise<void> => {},
			unsafe: (s: string): string => s,
		},
	);
	return {
		sql: sql as unknown as Pool["sql"],
		async connect() {
			return { ok: true as const, value: undefined };
		},
		async end() {
			recorder.calls.push("pool.end");
		},
	};
}

interface EmittedEvent {
	readonly type: Event["type"];
	readonly reason?: string;
}

function createStubBus(
	recorder: Recorder,
	emitted: EmittedEvent[],
	options?: { failOnStart?: boolean },
): EventBus {
	const handlers = new Map<string, (event: Event) => Promise<void> | void>();
	const bus: EventBus = {
		on(type, handler): void {
			handlers.set(type as string, handler as (event: Event) => Promise<void>);
		},
		onEphemeral(): () => void {
			return () => {};
		},
		async emit(event) {
			recorder.calls.push(`bus.emit:${event.type}`);
			const reason =
				typeof event.data === "object" &&
				event.data !== null &&
				"reason" in event.data &&
				typeof (event.data as { reason?: unknown }).reason === "string"
					? (event.data as { reason: string }).reason
					: undefined;
			const record = reason !== undefined ? { type: event.type, reason } : { type: event.type };
			emitted.push(record);
			return event as unknown as Event;
		},
		emitEphemeral(): void {},
		async start() {
			recorder.calls.push("bus.start");
			if (options?.failOnStart === true) {
				throw new Error("bus boom");
			}
		},
		async stop() {
			recorder.calls.push("bus.stop");
		},
		async flush() {},
	};
	return bus;
}

function createStubScheduler(recorder: Recorder): Scheduler {
	return {
		async start() {
			recorder.calls.push("scheduler.start");
		},
		async stop() {
			recorder.calls.push("scheduler.stop");
		},
		isRunning() {
			return false;
		},
		activeCount() {
			return 0;
		},
		async tick() {},
		async executeJob() {},
	} as unknown as Scheduler;
}

function createStubChatEngine(
	handler: (body: string, gate: string) => Promise<TurnResult>,
): ChatEngine {
	return {
		async handleMessage(body: string, gate: string) {
			return handler(body, gate);
		},
		abortCurrentTurn() {},
		async resetSession() {},
	} as unknown as ChatEngine;
}

function createStubGate(recorder: Recorder, onStart?: () => Promise<void>): Gate {
	let stopped = false;
	return {
		name: "stub",
		async start() {
			recorder.calls.push("gate.start");
			if (onStart) await onStart();
			// Hold until stop() resolves — emulates CLI waiting on user input.
			await new Promise<void>((resolve) => {
				const check = setInterval(() => {
					if (stopped) {
						clearInterval(check);
						resolve();
					}
				}, 5);
				// Unref the timer so Bun's test runner doesn't hold the process.
				(check as unknown as { unref?: () => void }).unref?.();
			});
		},
		async stop() {
			recorder.calls.push("gate.stop");
			stopped = true;
		},
	};
}

// The engine calls migrate(sql) directly — we replace the module via mock.module
// so no real migration runs. Using Bun's mock.module is overkill here; instead,
// we expose a MigrateStub via a separate path by structuring tests to avoid a
// real migration execution. Patch the import through mock.module.
const migrateFn = mock(async () => ({ ok: true as const, value: { applied: 0 } }));
mock.module("../src/db/migrate.ts", () => ({
	migrate: migrateFn,
}));

// ---------------------------------------------------------------------------
// Shared factory
// ---------------------------------------------------------------------------

interface Harness {
	engine: Engine;
	recorder: Recorder;
	emitted: EmittedEvent[];
	pool: Pool;
	bus: EventBus;
	scheduler: Scheduler;
	chatEngine: ChatEngine;
	gate: Gate;
}

function buildHarness(opts?: {
	chatHandler?: (body: string, gate: string) => Promise<TurnResult>;
	busFailOnStart?: boolean;
}): Harness {
	const recorder: Recorder = { calls: [] };
	const emitted: EmittedEvent[] = [];

	const pool = createStubPool(recorder);
	const bus = createStubBus(recorder, emitted, { failOnStart: opts?.busFailOnStart ?? false });
	const scheduler = createStubScheduler(recorder);
	const chatEngine = createStubChatEngine(
		opts?.chatHandler ?? (async (body) => ({ ok: true as const, response: `echo: ${body}` })),
	);
	const gate = createStubGate(recorder);
	const engine = new Engine({ pool, bus, scheduler, chatEngine, gate, version: "test" });
	return { engine, recorder, emitted, pool, bus, scheduler, chatEngine, gate };
}

// Short helper — wait for a predicate with a small bounded timeout.
async function waitFor(predicate: () => boolean, timeoutMs = 500): Promise<void> {
	const start = Date.now();
	while (!predicate()) {
		if (Date.now() - start > timeoutMs) {
			throw new Error("waitFor: timeout");
		}
		await new Promise((r) => setTimeout(r, 5));
	}
}

// ---------------------------------------------------------------------------
// State transitions
// ---------------------------------------------------------------------------

describe("Engine.start", () => {
	test("transitions stopped → starting → running and runs startup sequence", async () => {
		const h = buildHarness();
		expect(h.engine.state).toBe("stopped");
		const startPromise = h.engine.start();
		await startPromise;
		// Wait for the gate.start hook to fire (fire-and-forget in start()).
		await waitFor(() => h.recorder.calls.includes("gate.start"));
		expect(h.engine.state).toBe("running");

		// Startup emits system.started
		expect(h.emitted.map((e) => e.type)).toContain("system.started");

		// Sequence: bus.start < scheduler.start < system.started < gate.start.
		const idx = (c: string): number => h.recorder.calls.indexOf(c);
		expect(idx("bus.start")).toBeLessThan(idx("scheduler.start"));
		expect(idx("scheduler.start")).toBeLessThan(idx("bus.emit:system.started"));
		expect(idx("bus.emit:system.started")).toBeLessThan(idx("gate.start"));

		await h.engine.stop("test_teardown");
	});

	test("rejects start when not stopped", async () => {
		const h = buildHarness();
		await h.engine.start();
		await waitFor(() => h.recorder.calls.includes("gate.start"));
		await expect(h.engine.start()).rejects.toThrow(/invalid state/);
		await h.engine.stop("test_teardown");
	});

	test("rolls back to stopped on startup failure", async () => {
		const h = buildHarness({ busFailOnStart: true });
		await expect(h.engine.start()).rejects.toThrow(/bus boom/);
		expect(h.engine.state).toBe("stopped");
	});
});

describe("Engine.stop", () => {
	test("transitions running → stopping → stopped in reverse order", async () => {
		const h = buildHarness();
		await h.engine.start();
		await waitFor(() => h.recorder.calls.includes("gate.start"));
		await h.engine.stop("SIGTERM");
		expect(h.engine.state).toBe("stopped");

		// Teardown sequence: gate.stop → scheduler.stop → system.stopped → bus.stop → pool.end
		const idx = (c: string): number => h.recorder.calls.indexOf(c);
		expect(idx("gate.stop")).toBeLessThan(idx("scheduler.stop"));
		expect(idx("scheduler.stop")).toBeLessThan(idx("bus.emit:system.stopped"));
		expect(idx("bus.emit:system.stopped")).toBeLessThan(idx("bus.stop"));
		expect(idx("bus.stop")).toBeLessThan(idx("pool.end"));
	});

	test("emits system.stopped with the reason from the caller", async () => {
		const h = buildHarness();
		await h.engine.start();
		await waitFor(() => h.recorder.calls.includes("gate.start"));
		await h.engine.stop("SIGINT");
		const stopped = h.emitted.find((e) => e.type === "system.stopped");
		expect(stopped).toBeDefined();
		expect(stopped?.reason).toBe("SIGINT");
	});

	test("is a no-op when stop is re-entered during shutdown (signal race)", async () => {
		const h = buildHarness();
		await h.engine.start();
		await waitFor(() => h.recorder.calls.includes("gate.start"));
		// Fire two concurrent stop() calls to emulate SIGTERM+SIGINT back-to-back
		const [a, b] = await Promise.all([h.engine.stop("SIGTERM"), h.engine.stop("SIGINT")]);
		expect(a).toBeUndefined();
		expect(b).toBeUndefined();
		// system.stopped should have been emitted exactly once
		const stoppedEvents = h.emitted.filter((e) => e.type === "system.stopped");
		expect(stoppedEvents.length).toBe(1);
	});

	test("stop before start is a safe no-op — never-started engine does not touch subsystems", async () => {
		const h = buildHarness();
		await h.engine.stop("never_started");
		expect(h.engine.state).toBe("stopped");
		// Engine never owned the pool — the caller opened it and should close it.
		expect(h.recorder.calls).not.toContain("pool.end");
		// System.stopped is NOT emitted because the bus never started
		expect(h.emitted.some((e) => e.type === "system.stopped")).toBe(false);
	});
});

describe("Engine.pause / resume", () => {
	test("pause transitions running → paused", async () => {
		const h = buildHarness();
		await h.engine.start();
		await waitFor(() => h.recorder.calls.includes("gate.start"));
		h.engine.pause();
		expect(h.engine.state).toBe("paused");
		await h.engine.resume();
		await h.engine.stop("teardown");
	});

	test("messages sent while paused are queued, not processed", async () => {
		let calls = 0;
		const h = buildHarness({
			chatHandler: async () => {
				calls += 1;
				return { ok: true, response: "ok" };
			},
		});
		await h.engine.start();
		await waitFor(() => h.recorder.calls.includes("gate.start"));
		h.engine.pause();

		const pending = h.engine.handleMessage("hello", "cli");
		expect(h.engine.queuedMessageCount).toBe(1);
		expect(calls).toBe(0);

		// Resume should drain the queue in order.
		await h.engine.resume();
		const result = await pending;
		expect(result.ok).toBe(true);
		expect(calls).toBe(1);
		expect(h.engine.queuedMessageCount).toBe(0);

		await h.engine.stop("teardown");
	});

	test("resume drains multiple queued messages in arrival order", async () => {
		const order: string[] = [];
		const h = buildHarness({
			chatHandler: async (body) => {
				order.push(body);
				return { ok: true, response: body };
			},
		});
		await h.engine.start();
		await waitFor(() => h.recorder.calls.includes("gate.start"));
		h.engine.pause();

		const p1 = h.engine.handleMessage("first", "cli");
		const p2 = h.engine.handleMessage("second", "cli");
		const p3 = h.engine.handleMessage("third", "cli");

		expect(h.engine.queuedMessageCount).toBe(3);
		await h.engine.resume();
		await Promise.all([p1, p2, p3]);
		expect(order).toEqual(["first", "second", "third"]);

		await h.engine.stop("teardown");
	});

	test("pause rejects when engine is not running", async () => {
		const h = buildHarness();
		// From stopped
		expect(() => h.engine.pause()).toThrow(/invalid state/);
		await h.engine.stop("teardown");
	});

	test("stop while paused rejects queued messages with engine stopped error", async () => {
		const h = buildHarness();
		await h.engine.start();
		await waitFor(() => h.recorder.calls.includes("gate.start"));
		h.engine.pause();
		const pending = h.engine.handleMessage("will be rejected", "cli");
		await h.engine.stop("shutdown");
		await expect(pending).rejects.toThrow(/engine stopped/);
	});
});

describe("Engine.handleMessage", () => {
	test("forwards to chat engine when running", async () => {
		let captured: { body?: string; gate?: string } = {};
		const h = buildHarness({
			chatHandler: async (body, gate) => {
				captured = { body, gate };
				return { ok: true, response: "processed" };
			},
		});
		await h.engine.start();
		await waitFor(() => h.recorder.calls.includes("gate.start"));
		const result = await h.engine.handleMessage("ping", "cli");
		expect(result.ok).toBe(true);
		expect(captured).toEqual({ body: "ping", gate: "cli" });
		await h.engine.stop("teardown");
	});

	test("throws when the engine is stopped", async () => {
		const h = buildHarness();
		await expect(h.engine.handleMessage("ping", "cli")).rejects.toThrow(/invalid state/);
		await h.engine.stop("teardown");
	});
});

// ---------------------------------------------------------------------------
// Signal handlers
// ---------------------------------------------------------------------------

describe("installSignalHandlers", () => {
	test("SIGTERM triggers graceful shutdown exactly once", async () => {
		const h = buildHarness();
		await h.engine.start();
		await waitFor(() => h.recorder.calls.includes("gate.start"));
		const uninstall = installSignalHandlers(h.engine);
		try {
			process.emit("SIGTERM");
			// Wait for stop to complete
			await waitFor(() => h.engine.state === "stopped");
			expect(h.engine.state).toBe("stopped");
			const stopped = h.emitted.filter((e) => e.type === "system.stopped");
			expect(stopped.length).toBe(1);
			expect(stopped[0]?.reason).toBe("SIGTERM");
		} finally {
			uninstall();
		}
	});

	test("SIGINT triggers graceful shutdown with SIGINT reason", async () => {
		const h = buildHarness();
		await h.engine.start();
		await waitFor(() => h.recorder.calls.includes("gate.start"));
		const uninstall = installSignalHandlers(h.engine);
		try {
			process.emit("SIGINT");
			await waitFor(() => h.engine.state === "stopped");
			const stopped = h.emitted.filter((e) => e.type === "system.stopped");
			expect(stopped[0]?.reason).toBe("SIGINT");
		} finally {
			uninstall();
		}
	});
});
