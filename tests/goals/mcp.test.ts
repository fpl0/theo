/**
 * read_goals MCP tool tests — trust tier scoping + redaction masking.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { registerGoalHandlers } from "../../src/goals/handlers.ts";
import { readGoalsTool } from "../../src/goals/mcp.ts";
import { GoalRepository } from "../../src/goals/repository.ts";
import { newGoalTaskId } from "../../src/goals/types.ts";
import { NodeRepository } from "../../src/memory/graph/nodes.ts";
import type { TrustTier } from "../../src/memory/graph/types.ts";
import {
	cleanEventTables,
	createMockEmbeddings,
	createTestBus,
	createTestPool,
} from "../helpers.ts";

let pool: Pool;
let sql: Sql;
let bus: EventBus;
let nodes: NodeRepository;
let goals: GoalRepository;

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
	await sql`TRUNCATE events, handler_cursors, node, edge, goal_state, goal_task, resume_context CASCADE`;
	await cleanEventTables(sql);
	bus = createTestBus(sql);
	nodes = new NodeRepository(sql, bus, createMockEmbeddings());
	goals = new GoalRepository(sql, bus, nodes);
	registerGoalHandlers({ sql, bus, goals });
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

function toolCall(trust: TrustTier) {
	const tool = readGoalsTool({ goals, resolveTrust: () => trust });
	return tool.handler;
}

async function run(handler: ReturnType<typeof toolCall>, args: Record<string, unknown>) {
	return (await handler(
		args as Parameters<typeof handler>[0],
		{} as unknown as Parameters<typeof handler>[1],
	)) as { content: { type: string; text: string }[] };
}

async function createGoal(
	title: string,
	effectiveTrust: TrustTier,
	origin: "owner" | "ideation" | "reflex" | "system" = "owner",
): Promise<number> {
	const state = await goals.create({
		title,
		description: "desc",
		origin,
		effectiveTrust,
		actor: origin === "owner" ? "user" : "system",
	});
	return Number(state.nodeId);
}

describe("read_goals MCP tool", () => {
	test("owner tier sees all goals", async () => {
		await createGoal("owner goal", "owner");
		await createGoal("external goal", "external", "reflex");
		const handler = toolCall("owner");
		const result = await run(handler, { includePlan: false });
		expect(result.content[0]?.text).toContain("owner goal");
		expect(result.content[0]?.text).toContain("external goal");
	});

	test("external tier does not see owner goals", async () => {
		await createGoal("owner goal", "owner");
		await createGoal("external goal", "external", "reflex");
		const handler = toolCall("external");
		const result = await run(handler, { includePlan: false });
		expect(result.content[0]?.text).not.toContain("owner goal");
		expect(result.content[0]?.text).toContain("external goal");
	});

	test("redacted goal shows [redacted] tag", async () => {
		const nodeId = await createGoal("secret", "owner");
		await bus.emit({
			type: "goal.redacted",
			version: 1,
			actor: "user",
			data: {
				nodeId,
				redactedFields: ["title", "description"],
				redactedBy: "user",
			},
			metadata: {},
		});
		await bus.flush();
		const handler = toolCall("owner");
		const result = await run(handler, { includePlan: false });
		expect(result.content[0]?.text).toContain("[redacted]");
	});

	test("status filter narrows results", async () => {
		const active = await createGoal("active goal", "owner");
		const proposed = await createGoal("proposed goal", "inferred", "ideation");
		void active;
		void proposed;
		const handler = toolCall("owner");
		const result = await run(handler, { status: ["active"], includePlan: false });
		expect(result.content[0]?.text).toContain("active goal");
		expect(result.content[0]?.text).not.toContain("proposed goal");
	});

	test("includePlan=true renders plan steps", async () => {
		const nodeId = await createGoal("plan goal", "owner");
		const taskId = newGoalTaskId();
		await bus.emit({
			type: "goal.plan_updated",
			version: 1,
			actor: "theo",
			data: {
				nodeId,
				planVersion: 1,
				plan: [{ taskId, body: "first step body", dependsOn: [] }],
				reason: "initial",
				previousPlanHash: null,
			},
			metadata: {},
		});
		await bus.flush();
		const handler = toolCall("owner");
		const result = await run(handler, { includePlan: true });
		expect(result.content[0]?.text).toContain("first step body");
	});

	test("no goals visible returns informational text", async () => {
		const handler = toolCall("external");
		const result = await run(handler, { includePlan: false });
		expect(result.content[0]?.text).toContain("No goals visible");
	});
});
