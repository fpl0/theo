/**
 * Synthetic prober tests.
 *
 * The prober wraps a chat engine (stubbed as `ChatHandleLike`) and verifies
 * it emits `synthetic.probe.completed` with the correct `ok` + `durationMs`.
 * We use the real event bus here (driven by `createTestBus` via the helpers)
 * so the test covers the full emission path.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { TurnResult } from "../../src/chat/types.ts";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { initMetrics } from "../../src/telemetry/metrics.ts";
import {
	classifyProbeError,
	runProbe,
	SyntheticProbeScheduler,
} from "../../src/telemetry/synthetic.ts";
import { cleanEventTables, createTestBus, createTestPool } from "../helpers.ts";

let pool: Pool;
let sql: Sql;
let bus: EventBus;

beforeAll(async () => {
	pool = createTestPool();
	const connectResult = await pool.connect();
	if (!connectResult.ok) throw new Error(connectResult.error.message);
	sql = pool.sql;
});

afterAll(async () => {
	await pool.end();
});

beforeEach(async () => {
	await cleanEventTables(sql);
	bus = createTestBus(sql);
	await bus.start();
});

afterEach(async () => {
	try {
		await bus.flush();
	} catch {
		/* ignore */
	}
	try {
		await bus.stop();
	} catch {
		/* ignore */
	}
});

describe("runProbe", () => {
	test("successful turn emits synthetic.probe.completed with ok=true", async () => {
		const metrics = initMetrics({ environment: "test" });
		const chat = {
			handleMessage: async (_body: string, _gate: string): Promise<TurnResult> =>
				Promise.resolve({ ok: true, response: "pong" }),
		};
		await runProbe({ chat, bus, metrics, timeoutMs: 5_000 });
		const rows = await sql<Record<string, unknown>[]>`
			SELECT data FROM events WHERE type = 'synthetic.probe.completed'
		`;
		expect(rows.length).toBe(1);
		const data = (rows[0]?.["data"] ?? {}) as Record<string, unknown>;
		expect(data["ok"]).toBe(true);
		expect(typeof data["durationMs"]).toBe("number");
		const probeDurationSamples = metrics.meter.samplesFor("theo.synthetic.probe_duration_ms");
		expect(probeDurationSamples.length).toBe(1);
		expect(metrics.meter.samplesFor("theo.synthetic.probe_failures_total").length).toBe(0);
	});

	test("failed turn emits synthetic.probe.completed with ok=false and increments failure counter", async () => {
		const metrics = initMetrics({ environment: "test" });
		const chat = {
			handleMessage: async (_body: string, _gate: string): Promise<TurnResult> =>
				Promise.resolve({ ok: false, error: "blew up" }),
		};
		await runProbe({ chat, bus, metrics, timeoutMs: 5_000 });
		const rows = await sql<Record<string, unknown>[]>`
			SELECT data FROM events WHERE type = 'synthetic.probe.completed'
		`;
		expect(rows.length).toBe(1);
		const data = (rows[0]?.["data"] ?? {}) as Record<string, unknown>;
		expect(data["ok"]).toBe(false);
		expect(data["reason"]).toBe("blew up");
		const failures = metrics.meter.samplesFor("theo.synthetic.probe_failures_total");
		expect(failures.length).toBe(1);
	});

	test("thrown exception is classified and captured", async () => {
		const metrics = initMetrics({ environment: "test" });
		const chat = {
			handleMessage: async (_body: string, _gate: string): Promise<TurnResult> => {
				throw new Error("kaboom");
			},
		};
		await runProbe({ chat, bus, metrics, timeoutMs: 5_000 });
		const rows = await sql<Record<string, unknown>[]>`
			SELECT data FROM events WHERE type = 'synthetic.probe.completed'
		`;
		expect(rows.length).toBe(1);
		const data = (rows[0]?.["data"] ?? {}) as Record<string, unknown>;
		expect(data["ok"]).toBe(false);
		expect(data["reason"]).toBe("exception");
	});
});

describe("classifyProbeError", () => {
	test("timeout message classifies as 'timeout'", () => {
		expect(classifyProbeError(new Error("probe timeout"))).toBe("timeout");
	});
	test("generic error classifies as 'exception'", () => {
		expect(classifyProbeError(new Error("something else"))).toBe("exception");
	});
});

describe("SyntheticProbeScheduler", () => {
	test("start + stop does not throw", async () => {
		const metrics = initMetrics({ environment: "test" });
		const chat = {
			handleMessage: async (_body: string, _gate: string): Promise<TurnResult> =>
				Promise.resolve({ ok: true, response: "pong" }),
		};
		const scheduler = new SyntheticProbeScheduler(
			{ chat, bus, metrics, timeoutMs: 1_000 },
			{ intervalMs: 60_000 },
		);
		scheduler.start();
		// Starting twice is idempotent.
		scheduler.start();
		await scheduler.stop();
		expect(true).toBe(true);
	});
});
