/**
 * SLO pre-merge gate tests.
 *
 * We stub `fetch` so the tests never touch the network. The assertions
 * cover the three decision paths: pass, budget-exhausted, fast-burn.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { checkSlosBeforeMerge, type SloDefinition } from "../../src/selfupdate/slo_gate.ts";
import { cleanEventTables, createTestBus, createTestPool } from "../helpers.ts";

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

const SLO: SloDefinition = {
	id: "turn_available",
	budgetRemainingSeries: "theo:slo:error_budget_remaining_ratio",
	burnRate1hSeries: "theo:slo:turns_available:burn_rate_1h",
	fastBurnThreshold: 14.4,
	budgetFloor: 0.1,
};

function fakePromResponse(budget: number, burnRate: number): typeof fetch {
	return (async (url: Request | string | URL): Promise<Response> => {
		const query = new URL(
			typeof url === "string" ? url : url instanceof URL ? url.toString() : url.url,
		).searchParams.get("query");
		const value = query?.includes("budget") === true ? budget : burnRate;
		return new Response(
			JSON.stringify({
				status: "success",
				data: {
					result: [{ metric: {}, value: [Date.now() / 1000, String(value)] }],
				},
			}),
			{ headers: { "content-type": "application/json" } },
		);
	}) as unknown as typeof fetch;
}

describe("checkSlosBeforeMerge", () => {
	test("passes when budget is high and burn rate is normal", async () => {
		const decision = await checkSlosBeforeMerge({
			prometheusUrl: "http://prom:9090",
			slos: [SLO],
			fetcher: fakePromResponse(0.5, 1.0),
		});
		expect(decision.ok).toBe(true);
		expect(decision.measurements[0]?.blocked).toBe(false);
	});

	test("blocks when budget is below the floor", async () => {
		const decision = await checkSlosBeforeMerge({
			prometheusUrl: "http://prom:9090",
			slos: [SLO],
			fetcher: fakePromResponse(0.05, 1.0),
			bus,
		});
		expect(decision.ok).toBe(false);
		expect(decision.measurements[0]?.blocked).toBe(true);
		expect(decision.measurements[0]?.reason).toBe("budget_exhausted");

		// `self_update.blocked` was emitted.
		const events = await sql<Record<string, unknown>[]>`
			SELECT data FROM events WHERE type = 'self_update.blocked'
		`;
		expect(events.length).toBe(1);
		const data = (events[0]?.["data"] ?? {}) as Record<string, unknown>;
		expect(data["slo"]).toBe("turn_available");
		expect(data["reason"]).toBe("budget_exhausted");
	});

	test("blocks when burn rate exceeds fast-burn threshold", async () => {
		const decision = await checkSlosBeforeMerge({
			prometheusUrl: "http://prom:9090",
			slos: [SLO],
			fetcher: fakePromResponse(0.5, 20.0),
		});
		expect(decision.ok).toBe(false);
		expect(decision.measurements[0]?.reason).toBe("fast_burn");
	});

	test("treats Prometheus unreachable as a safety failure", async () => {
		const failing = (async (): Promise<Response> => {
			throw new Error("ECONNREFUSED");
		}) as unknown as typeof fetch;
		const decision = await checkSlosBeforeMerge({
			prometheusUrl: "http://prom:9090",
			slos: [SLO],
			fetcher: failing,
		});
		expect(decision.ok).toBe(false);
		expect(decision.measurements[0]?.blocked).toBe(true);
	});
});
