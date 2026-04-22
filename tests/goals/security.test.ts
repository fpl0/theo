/**
 * Security / trust-tier enforcement tests.
 *
 * Covers:
 *   - External-trust goals' subagent dispatch uses a read-only tool allowlist.
 *   - Trust tier inherited from causation chain is stored on goal_state.
 *   - Ideation-origin goals start as `proposed` — cannot auto-elevate.
 *   - `read_goals` trust filter hides owner goals from external-trust turns.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type {
	McpSdkServerConfigWithInstance,
	Options,
	Query,
	SDKMessage,
} from "@anthropic-ai/claude-agent-sdk";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { ExecutiveLoop, type ExecutiveSubagent } from "../../src/goals/executive.ts";
import { registerGoalHandlers } from "../../src/goals/handlers.ts";
import { GoalLease } from "../../src/goals/lease.ts";
import { GoalRepository } from "../../src/goals/repository.ts";
import { newGoalRunnerId, newGoalTaskId, type PlanStep } from "../../src/goals/types.ts";
import { NodeRepository } from "../../src/memory/graph/nodes.ts";
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
let lease: GoalLease;

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
	lease = new GoalLease({ sql, bus, goals });
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

const stubMemoryServer: McpSdkServerConfigWithInstance = {
	type: "sdk",
	name: "memory",
	instance: {} as unknown as McpSdkServerConfigWithInstance["instance"],
};

const stubSubagents: Readonly<Record<string, ExecutiveSubagent>> = {
	planner: {
		model: "claude-sonnet-4-6",
		maxTurns: 10,
		systemPromptPrefix: "plan things",
	},
};

function successGenerator(): Query {
	async function* generator(): AsyncGenerator<SDKMessage> {
		yield {
			type: "result",
			subtype: "success",
			duration_ms: 1,
			duration_api_ms: 1,
			is_error: false,
			num_turns: 1,
			result: "ok",
			stop_reason: "end_turn",
			total_cost_usd: 0.001,
			usage: {
				input_tokens: 5,
				output_tokens: 5,
				cache_creation_input_tokens: 0,
				cache_read_input_tokens: 0,
			},
			modelUsage: {},
			permission_denials: [],
			uuid: "00000000-0000-0000-0000-000000000000",
			session_id: "s",
		} as unknown as SDKMessage;
	}
	const gen = generator() as unknown as Query;
	(gen as unknown as { interrupt?: () => Promise<void> }).interrupt = async () => {};
	return gen;
}

describe("goal security", () => {
	test("effective_trust inherited from causation chain at creation", async () => {
		const state = await goals.create({
			title: "external goal",
			description: "from webhook",
			origin: "reflex",
			effectiveTrust: "external",
			actor: "system",
		});
		expect(state.effectiveTrust).toBe("external");
		// The node should also be stored with `trust = external`.
		const node = await nodes.getById(state.nodeId);
		expect(node?.trust).toBe("external");
	});

	test("external-trust dispatch uses read-only allowlist", async () => {
		const state = await goals.create({
			title: "ext",
			description: "",
			origin: "reflex",
			effectiveTrust: "external",
			actor: "system",
		});
		// Manually confirm so the external goal becomes active.
		await bus.emit({
			type: "goal.confirmed",
			version: 1,
			actor: "system",
			data: { nodeId: Number(state.nodeId), confirmedBy: "system" },
			metadata: {},
		});
		await bus.flush();

		const taskId = newGoalTaskId();
		const plan: readonly PlanStep[] = [{ taskId, body: "do", dependsOn: [] }];

		const capturedOptions: Options[] = [];
		const queryFn = (params: { prompt: string; options?: Options }): Query => {
			if (params.options) capturedOptions.push(params.options);
			return successGenerator();
		};
		const runnerId = newGoalRunnerId();
		const loop = new ExecutiveLoop({
			bus,
			goals,
			lease,
			memoryServer: stubMemoryServer,
			subagents: stubSubagents,
			queryFn,
			planner: async () => plan,
		});

		// First turn: plan.
		await loop.executeOneTurn({ runnerId });
		await bus.flush();

		// Second turn: dispatch (this is where external-trust shows up).
		await loop.executeOneTurn({ runnerId });
		await bus.flush();

		// At least one dispatch must have happened; the external-trust goal
		// should restrict tools to read-only.
		const dispatched = capturedOptions.find(
			(opts) =>
				Array.isArray(opts.allowedTools) && opts.allowedTools.every((t) => t !== "mcp__memory__*"),
		);
		expect(dispatched).toBeDefined();
		expect(dispatched?.allowedTools).toContain("mcp__memory__search_memory");
		expect(dispatched?.allowedTools).not.toContain("mcp__memory__store_memory");
	});

	test("ideation-origin goal starts proposed (cannot auto-escalate)", async () => {
		const state = await goals.create({
			title: "idea",
			description: "",
			origin: "ideation",
			effectiveTrust: "inferred",
			actor: "system",
		});
		expect(state.status).toBe("proposed");
		// Lease acquisition should not pick it up (status != active).
		const leased = await lease.acquire(newGoalRunnerId());
		expect(leased).toBeNull();
	});

	test("read_goals trust filter hides owner goals from external-tier turns", async () => {
		await goals.create({
			title: "owner secret",
			description: "",
			origin: "owner",
			effectiveTrust: "owner",
			actor: "user",
		});
		await goals.create({
			title: "ext visible",
			description: "",
			origin: "reflex",
			effectiveTrust: "external",
			actor: "system",
		});

		const externalView = await goals.listByTrust("external");
		const titles = await Promise.all(externalView.map((g) => goals.readTitle(g.nodeId)));
		expect(titles.some((t) => t?.includes("owner secret") ?? false)).toBe(false);
		expect(titles.some((t) => t?.includes("ext visible") ?? false)).toBe(true);
	});
});
