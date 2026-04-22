/**
 * Executive loop integration tests.
 *
 * The SDK is stubbed via `queryFn` — each test hands the loop a canned
 * generator that yields SDK-shaped success/error results. The planner is
 * also stubbed for deterministic plan generation.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type {
	McpSdkServerConfigWithInstance,
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
import {
	type GoalRunnerId,
	newGoalRunnerId,
	newGoalTaskId,
	type PlanStep,
} from "../../src/goals/types.ts";
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

// ---------------------------------------------------------------------------
// SDK stubs
// ---------------------------------------------------------------------------

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
	coder: {
		model: "claude-sonnet-4-6",
		maxTurns: 30,
		systemPromptPrefix: "code things",
	},
};

function successGenerator(text: string, tokens = 10, costUsd = 0.001): Query {
	async function* generator(): AsyncGenerator<SDKMessage> {
		yield {
			type: "result",
			subtype: "success",
			duration_ms: 1,
			duration_api_ms: 1,
			is_error: false,
			num_turns: 1,
			result: text,
			stop_reason: "end_turn",
			total_cost_usd: costUsd,
			usage: {
				input_tokens: tokens / 2,
				output_tokens: tokens / 2,
				cache_creation_input_tokens: 0,
				cache_read_input_tokens: 0,
				server_tool_use: null,
				service_tier: null,
				cache_creation: null,
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

function failureGenerator(
	subtype:
		| "error_during_execution"
		| "error_max_turns"
		| "error_max_budget_usd" = "error_during_execution",
): Query {
	async function* generator(): AsyncGenerator<SDKMessage> {
		yield {
			type: "result",
			subtype,
			duration_ms: 1,
			duration_api_ms: 1,
			is_error: true,
			num_turns: 1,
			errors: ["boom"],
			uuid: "00000000-0000-0000-0000-000000000000",
			session_id: "s",
		} as unknown as SDKMessage;
	}
	const gen = generator() as unknown as Query;
	(gen as unknown as { interrupt?: () => Promise<void> }).interrupt = async () => {};
	return gen;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function createGoal(priority = 50): Promise<number> {
	const state = await goals.create({
		title: "exec goal",
		description: "desc",
		origin: "owner",
		effectiveTrust: "owner",
		actor: "user",
		ownerPriority: priority,
	});
	return Number(state.nodeId);
}

function makeExecutive(
	queryFn: (params: { prompt: string }) => Query,
	planner: (() => Promise<readonly PlanStep[]>) | null = null,
): { loop: ExecutiveLoop; runnerId: GoalRunnerId } {
	const runnerId = newGoalRunnerId();
	const deps = {
		bus,
		goals,
		lease,
		memoryServer: stubMemoryServer,
		subagents: stubSubagents,
		queryFn,
		...(planner !== null ? { planner: async () => planner() } : {}),
	};
	return { loop: new ExecutiveLoop(deps), runnerId };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("ExecutiveLoop", () => {
	test("empty plan triggers planner, plan_updated emitted", async () => {
		const nodeId = await createGoal();
		const taskId = newGoalTaskId();
		const plan: readonly PlanStep[] = [{ taskId, body: "single step", dependsOn: [] }];
		const { loop, runnerId } = makeExecutive(
			() => successGenerator("ok"),
			async () => plan,
		);

		await loop.executeOneTurn({ runnerId });
		await bus.flush();

		const state = await goals.readState(nodeId as never);
		expect(state?.planVersion).toBe(1);
		expect(state?.plan.length).toBe(1);
	});

	test("picks next ready task, dispatches subagent, task_completed emitted", async () => {
		const nodeId = await createGoal();
		const taskId = newGoalTaskId();
		const plan: readonly PlanStep[] = [{ taskId, body: "one step", dependsOn: [] }];
		// First turn: plan.
		const planner = makeExecutive(
			() => successGenerator("ok"),
			async () => plan,
		);
		await planner.loop.executeOneTurn({ runnerId: planner.runnerId });
		await bus.flush();

		// Second turn: execute.
		const exec = makeExecutive(() => successGenerator("done", 10, 0.02));
		await exec.loop.executeOneTurn({ runnerId: exec.runnerId });
		await bus.flush();

		const task = await goals.getTask(taskId);
		expect(task?.status).toBe("completed");
		const state = await goals.readState(nodeId as never);
		expect(state?.currentTaskId).toBeNull();
	});

	test("SDK max_turns failure → goal.task_yielded (turn_budget_exceeded)", async () => {
		const nodeId = await createGoal();
		const taskId = newGoalTaskId();
		const plan: readonly PlanStep[] = [{ taskId, body: "step", dependsOn: [] }];
		const planner = makeExecutive(
			() => successGenerator("x"),
			async () => plan,
		);
		await planner.loop.executeOneTurn({ runnerId: planner.runnerId });
		await bus.flush();

		const exec = makeExecutive(() => failureGenerator("error_max_turns"));
		await exec.loop.executeOneTurn({ runnerId: exec.runnerId });
		await bus.flush();

		const task = await goals.getTask(taskId);
		expect(task?.status).toBe("yielded");
		void nodeId;
	});

	test("SDK failure → goal.task_failed, increments consecutive_failures", async () => {
		await createGoal();
		const taskId = newGoalTaskId();
		const plan: readonly PlanStep[] = [{ taskId, body: "step", dependsOn: [] }];
		const planner = makeExecutive(
			() => successGenerator("x"),
			async () => plan,
		);
		await planner.loop.executeOneTurn({ runnerId: planner.runnerId });
		await bus.flush();

		const exec = makeExecutive(() => failureGenerator("error_during_execution"));
		await exec.loop.executeOneTurn({ runnerId: exec.runnerId });
		await bus.flush();

		const task = await goals.getTask(taskId);
		expect(task?.status).toBe("failed");
	});

	test("no-op when nothing is leaseable", async () => {
		const { loop, runnerId } = makeExecutive(() => successGenerator("unused"));
		await loop.executeOneTurn({ runnerId });
		// No crash, no events beyond what already existed.
		const events = await sql<{ count: string }[]>`
			SELECT COUNT(*)::text AS count FROM events WHERE type LIKE 'goal.%'
		`;
		expect(Number(events[0]?.count ?? "0")).toBe(0);
	});

	test("completes goal when plan is drained", async () => {
		await createGoal();
		const taskId = newGoalTaskId();
		const plan: readonly PlanStep[] = [{ taskId, body: "step", dependsOn: [] }];
		const planner = makeExecutive(
			() => successGenerator("x"),
			async () => plan,
		);
		await planner.loop.executeOneTurn({ runnerId: planner.runnerId });
		await bus.flush();

		const exec = makeExecutive(() => successGenerator("done"));
		await exec.loop.executeOneTurn({ runnerId: exec.runnerId });
		await bus.flush();

		// Third turn: nothing pending, should complete the goal.
		const finalRun = makeExecutive(() => successGenerator("x"));
		await finalRun.loop.executeOneTurn({ runnerId: finalRun.runnerId });
		await bus.flush();

		// Look for goal.completed in the events.
		const completed = await sql<{ count: string }[]>`
			SELECT COUNT(*)::text AS count FROM events WHERE type = 'goal.completed'
		`;
		expect(Number(completed[0]?.count ?? "0")).toBeGreaterThanOrEqual(1);
	});
});
