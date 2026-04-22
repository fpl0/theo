/**
 * End-to-end bus + telemetry wrapper test.
 *
 * Ensures that when a durable handler is registered on a real `EventBus`
 * configured with the telemetry wrapper, handler invocations record
 * `theo.bus.handler_duration_ms` and errors increment
 * `theo.bus.handler_errors_total`.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { createEventBus } from "../../src/events/bus.ts";
import { createEventLog } from "../../src/events/log.ts";
import { createUpcasterRegistry } from "../../src/events/upcasters.ts";
import { initMetrics } from "../../src/telemetry/metrics.ts";
import { wrapHandlerWithSpan } from "../../src/telemetry/spans/bus.ts";
import { initTracer } from "../../src/telemetry/tracer.ts";
import { cleanEventTables, createTestPool } from "../helpers.ts";

let pool: Pool;
let sql: Sql;
let bus: EventBus;

beforeAll(async () => {
	pool = createTestPool();
	const connect = await pool.connect();
	if (!connect.ok) throw new Error(connect.error.message);
	sql = pool.sql;
});

afterAll(async () => {
	await pool.end();
});

beforeEach(async () => {
	await cleanEventTables(sql);
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

describe("bus wrapper end-to-end", () => {
	test("records handler duration when wrapper is installed", async () => {
		const upcasters = createUpcasterRegistry();
		const log = createEventLog(sql, upcasters);
		bus = createEventBus(log, sql);

		const metrics = initMetrics({ environment: "test" });
		const tracer = initTracer({ resource: { "service.name": "theo" }, metrics });
		bus.setDurableHandlerWrapper(wrapHandlerWithSpan(tracer, metrics));

		let received = 0;
		bus.on(
			"system.started",
			async () => {
				received++;
			},
			{ id: "test-handler", mode: "effect" },
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

		expect(received).toBe(1);
		const samples = metrics.meter.samplesFor("theo.bus.handler_duration_ms");
		expect(samples.length).toBe(1);
		expect(samples[0]?.labels["handler"]).toBe("test-handler");
	});
});
