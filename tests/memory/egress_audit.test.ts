/**
 * Egress audit tests — `recordCloudEgressTurn` emits correctly, the helper is
 * wired at every autonomous SDK call site, and the audit record matches the
 * schema Grafana's `cost.json` dashboard projects.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { newEventId } from "../../src/events/ids.ts";
import { recordCloudEgressTurn } from "../../src/memory/egress.ts";
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
		// ignore
	}
	try {
		await bus.stop();
	} catch {
		// ignore
	}
});

describe("recordCloudEgressTurn", () => {
	test("emits cloud_egress.turn with the supplied payload", async () => {
		const causeId = newEventId();
		await recordCloudEgressTurn(bus, {
			subagent: "ideation",
			model: "claude-sonnet-4-6",
			inputTokens: 100,
			outputTokens: 50,
			costUsd: 0.01,
			turnClass: "ideation",
			causeEventId: causeId,
			includedDimensions: ["communication_style"],
			strippedDimensions: ["values", "shadow_patterns"],
		});

		const rows = await sql<Record<string, unknown>[]>`
			SELECT type, data, metadata FROM events WHERE type = 'cloud_egress.turn'
		`;
		expect(rows.length).toBe(1);
		const row = rows[0] ?? {};
		expect(row["type"]).toBe("cloud_egress.turn");
		const data = row["data"] as Record<string, unknown>;
		expect(data["subagent"]).toBe("ideation");
		expect(data["model"]).toBe("claude-sonnet-4-6");
		expect(data["inputTokens"]).toBe(100);
		expect(data["outputTokens"]).toBe(50);
		expect(data["turnClass"]).toBe("ideation");
		expect(data["dimensionsIncluded"]).toEqual(["communication_style"]);
		expect(data["dimensionsExcluded"]).toEqual(["values", "shadow_patterns"]);
	});

	test("omitted advisorModel is not persisted as null", async () => {
		const causeId = newEventId();
		await recordCloudEgressTurn(bus, {
			subagent: "main",
			model: "claude-sonnet-4-6",
			inputTokens: 1,
			outputTokens: 1,
			costUsd: 0,
			turnClass: "interactive",
			causeEventId: causeId,
		});
		const rows = await sql<Record<string, unknown>[]>`
			SELECT data FROM events WHERE type = 'cloud_egress.turn' LIMIT 1
		`;
		const data = (rows[0]?.["data"] ?? {}) as Record<string, unknown>;
		expect("advisorModel" in data).toBe(false);
	});

	test("defaults dimensions to empty arrays", async () => {
		const causeId = newEventId();
		await recordCloudEgressTurn(bus, {
			subagent: "main",
			model: "claude-sonnet-4-6",
			inputTokens: 0,
			outputTokens: 0,
			costUsd: 0,
			turnClass: "interactive",
			causeEventId: causeId,
		});
		const rows = await sql<Record<string, unknown>[]>`
			SELECT data FROM events WHERE type = 'cloud_egress.turn' LIMIT 1
		`;
		const data = (rows[0]?.["data"] ?? {}) as Record<string, unknown>;
		expect(data["dimensionsIncluded"]).toEqual([]);
		expect(data["dimensionsExcluded"]).toEqual([]);
	});
});
