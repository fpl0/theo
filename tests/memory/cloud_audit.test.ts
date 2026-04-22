/**
 * Cloud egress audit — integration test against the real event log.
 */

import { afterAll, beforeEach, describe, expect, test } from "bun:test";
import { auditCloudEgress } from "../../src/memory/cloud_audit.ts";
import { cleanEventTables, createTestBus, createTestPool } from "../helpers.ts";

const pool = createTestPool();
const bus = createTestBus(pool.sql);

beforeEach(async () => {
	await cleanEventTables(pool.sql);
});

afterAll(async () => {
	await pool.end();
});

describe("auditCloudEgress", () => {
	test("sums costs per turn class across the window", async () => {
		await bus.emit({
			type: "cloud_egress.turn",
			version: 1,
			actor: "system",
			data: {
				subagent: "scanner",
				model: "haiku",
				inputTokens: 100,
				outputTokens: 50,
				costUsd: 0.001,
				dimensionsIncluded: [],
				dimensionsExcluded: [],
				turnClass: "reflex",
				causeEventId: "cause-1" as never,
			},
			metadata: {},
		});
		await bus.emit({
			type: "cloud_egress.turn",
			version: 1,
			actor: "system",
			data: {
				subagent: "main",
				model: "sonnet",
				inputTokens: 1000,
				outputTokens: 500,
				costUsd: 0.05,
				dimensionsIncluded: [],
				dimensionsExcluded: [],
				turnClass: "ideation",
				causeEventId: "cause-2" as never,
			},
			metadata: {},
		});

		const entries = await auditCloudEgress(pool.sql, "week");
		const byClass = new Map(entries.map((e) => [e.turnClass, e]));
		expect(byClass.get("reflex")?.costUsd).toBeCloseTo(0.001, 6);
		expect(byClass.get("ideation")?.costUsd).toBeCloseTo(0.05, 6);
	});

	test("empty window returns empty list", async () => {
		const entries = await auditCloudEgress(pool.sql, "day");
		expect(entries).toEqual([]);
	});
});
