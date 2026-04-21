/**
 * Integration tests for the scheduler MCP tool wrappers.
 *
 * The MCP tool instances expose a handler function we can call directly —
 * we skip the SDK's RPC layer entirely, which keeps tests tight and lets
 * us assert on the resulting database state.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { createJobStore, type JobStore } from "../../src/scheduler/store.ts";
import { cancelJobTool, listJobsTool, scheduleJobTool } from "../../src/scheduler/tools.ts";
import { cleanEventTables, createTestBus, createTestPool } from "../helpers.ts";

let pool: Pool;
let store: JobStore;

beforeAll(async () => {
	pool = createTestPool();
	const connectResult = await pool.connect();
	if (!connectResult.ok) throw new Error(`Test setup failed: ${connectResult.error.message}`);
	store = createJobStore(pool.sql);
});

afterAll(async () => {
	if (pool) await pool.end();
});

let bus: EventBus;

beforeEach(async () => {
	await pool.sql`TRUNCATE job_execution, scheduled_job CASCADE`;
	await cleanEventTables(pool.sql);
	bus = createTestBus(pool.sql);
});

afterEach(async () => {
	await bus.flush();
	await bus.stop();
});

async function countEvents(type: string): Promise<number> {
	const rows = await pool.sql<{ count: string }[]>`
		SELECT COUNT(*)::text AS count FROM events WHERE type = ${type}
	`;
	return Number(rows[0]?.count ?? 0);
}

// `tool()` returns an SDK tool record with a `handler` function. Typing it
// loosely keeps us away from the SDK's private generics.
interface SdkToolLike {
	readonly handler: (
		args: Record<string, unknown>,
		extra: Record<string, unknown>,
	) => Promise<CallToolResult>;
}

function asHandler(t: unknown): SdkToolLike["handler"] {
	const record = t as { handler: SdkToolLike["handler"] };
	return record.handler;
}

describe("schedule_job tool", () => {
	test("creates a cron job and returns a confirmation", async () => {
		const handler = asHandler(
			scheduleJobTool({ store, now: () => new Date(Date.UTC(2026, 0, 15, 12, 0, 0)) }),
		);
		const result = await handler(
			{
				name: "hourly",
				cron: "0 * * * *",
				agent: "main",
				prompt: "do hourly work",
				maxDurationMs: 60_000,
				maxBudgetUsd: 0.05,
			},
			{},
		);
		expect(result.isError).toBeFalsy();
		const job = await store.getByName("hourly");
		expect(job).not.toBeNull();
		expect(job?.cron).toBe("0 * * * *");
		expect(job?.nextRunAt.toISOString()).toBe("2026-01-15T13:00:00.000Z");
	});

	test("rejects invalid cron expressions", async () => {
		const handler = asHandler(scheduleJobTool({ store }));
		const result = await handler(
			{
				name: "bad",
				cron: "not a cron",
				agent: "main",
				prompt: "x",
				maxDurationMs: 60_000,
				maxBudgetUsd: 0.05,
			},
			{},
		);
		expect(result.isError).toBe(true);
	});

	test("creates a one-off job when cron=null and runAt provided", async () => {
		const handler = asHandler(scheduleJobTool({ store }));
		const runAt = "2026-03-01T00:00:00.000Z";
		const result = await handler(
			{
				name: "one-off",
				cron: null,
				agent: "main",
				prompt: "once",
				maxDurationMs: 30_000,
				maxBudgetUsd: 0.05,
				runAt,
			},
			{},
		);
		expect(result.isError).toBeFalsy();
		const job = await store.getByName("one-off");
		expect(job?.cron).toBeNull();
		expect(job?.nextRunAt.toISOString()).toBe(runAt);
	});

	test("rejects one-off jobs missing runAt", async () => {
		const handler = asHandler(scheduleJobTool({ store }));
		const result = await handler(
			{
				name: "one-off-bad",
				cron: null,
				agent: "main",
				prompt: "once",
				maxDurationMs: 30_000,
				maxBudgetUsd: 0.05,
			},
			{},
		);
		expect(result.isError).toBe(true);
	});
});

describe("list_jobs tool", () => {
	test("returns a textual summary of each job", async () => {
		const schedule = asHandler(scheduleJobTool({ store }));
		await schedule(
			{
				name: "one",
				cron: "0 * * * *",
				agent: "main",
				prompt: "p",
				maxDurationMs: 60_000,
				maxBudgetUsd: 0.05,
			},
			{},
		);
		const list = asHandler(listJobsTool({ store }));
		const result = await list({ enabledOnly: false }, {});
		expect(result.isError).toBeFalsy();
		const firstBlock = result.content[0];
		const text = firstBlock?.type === "text" ? firstBlock.text : "";
		expect(text).toContain("one");
	});

	test("enabledOnly filters disabled jobs", async () => {
		const schedule = asHandler(scheduleJobTool({ store }));
		await schedule(
			{
				name: "enabled",
				cron: "0 * * * *",
				agent: "main",
				prompt: "p",
				maxDurationMs: 60_000,
				maxBudgetUsd: 0.05,
			},
			{},
		);
		await schedule(
			{
				name: "disabled",
				cron: "0 * * * *",
				agent: "main",
				prompt: "p",
				maxDurationMs: 60_000,
				maxBudgetUsd: 0.05,
			},
			{},
		);
		const disabledJob = await store.getByName("disabled");
		if (disabledJob === null) throw new Error("seed failed");
		await store.disable(disabledJob.id);

		const list = asHandler(listJobsTool({ store }));
		const result = await list({ enabledOnly: true }, {});
		const firstBlock = result.content[0];
		const text = firstBlock?.type === "text" ? firstBlock.text : "";
		expect(text).toContain("enabled");
		expect(text).not.toContain("disabled");
	});
});

describe("cancel_job tool", () => {
	test("disables a job by name", async () => {
		const schedule = asHandler(scheduleJobTool({ store }));
		await schedule(
			{
				name: "to-cancel",
				cron: "0 * * * *",
				agent: "main",
				prompt: "p",
				maxDurationMs: 60_000,
				maxBudgetUsd: 0.05,
			},
			{},
		);
		const cancel = asHandler(cancelJobTool({ store }));
		const result = await cancel({ name: "to-cancel" }, {});
		expect(result.isError).toBeFalsy();
		const job = await store.getByName("to-cancel");
		expect(job?.enabled).toBe(false);
	});

	test("returns error for unknown job names", async () => {
		const cancel = asHandler(cancelJobTool({ store }));
		const result = await cancel({ name: "nope" }, {});
		expect(result.isError).toBe(true);
	});
});

describe("event emission when bus is provided", () => {
	test("schedule_job emits job.created", async () => {
		const schedule = asHandler(scheduleJobTool({ store, bus }));
		await schedule(
			{
				name: "emit-created",
				cron: "0 * * * *",
				agent: "main",
				prompt: "p",
				maxDurationMs: 60_000,
				maxBudgetUsd: 0.05,
			},
			{},
		);
		await bus.flush();
		expect(await countEvents("job.created")).toBe(1);
	});

	test("cancel_job emits job.cancelled", async () => {
		const schedule = asHandler(scheduleJobTool({ store, bus }));
		await schedule(
			{
				name: "emit-cancelled",
				cron: "0 * * * *",
				agent: "main",
				prompt: "p",
				maxDurationMs: 60_000,
				maxBudgetUsd: 0.05,
			},
			{},
		);
		const cancel = asHandler(cancelJobTool({ store, bus }));
		await cancel({ name: "emit-cancelled" }, {});
		await bus.flush();
		expect(await countEvents("job.cancelled")).toBe(1);
	});
});
