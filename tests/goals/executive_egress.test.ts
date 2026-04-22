/**
 * Executive egress wiring — verifies `cloud_egress.turn` is emitted after a
 * successful subagent dispatch when `userModel` + `sql` are supplied, and
 * that consent denial fails the task with `consent_denied`.
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
import { newGoalRunnerId, newGoalTaskId, type PlanStep } from "../../src/goals/types.ts";
import { NodeRepository } from "../../src/memory/graph/nodes.ts";
import type { UserModelRepository } from "../../src/memory/user_model.ts";
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
	const connect = await pool.connect();
	if (!connect.ok) throw new Error(connect.error.message);
	sql = pool.sql;
});

afterAll(async () => {
	await pool.end();
});

beforeEach(async () => {
	await sql`TRUNCATE events, handler_cursors, node, edge, goal_state, goal_task, resume_context, consent_ledger CASCADE`;
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
		/* ignore */
	}
	try {
		await bus.stop();
	} catch {
		/* ignore */
	}
});

// Minimal user-model stub — returns a single public dimension.
const stubUserModel: UserModelRepository = {
	getDimensions: async () => [
		{
			id: 1,
			name: "communication_style",
			value: "direct",
			confidence: 1.0,
			evidenceCount: 10,
			threshold: 5,
			egressSensitivity: "public",
			createdAt: new Date(),
			updatedAt: new Date(),
		},
	],
	getDimension: async () => null,
	updateDimension: async () => {
		throw new Error("not used");
	},
};

const stubMemoryServer: McpSdkServerConfigWithInstance = {
	type: "sdk",
	name: "memory",
	instance: {} as unknown as McpSdkServerConfigWithInstance["instance"],
};

const stubSubagents: Readonly<Record<string, ExecutiveSubagent>> = {
	planner: { model: "claude-sonnet-4-6", maxTurns: 10, systemPromptPrefix: "plan" },
	coder: { model: "claude-sonnet-4-6", maxTurns: 30, systemPromptPrefix: "code" },
};

function successGenerator(text: string): Query {
	async function* gen(): AsyncGenerator<SDKMessage> {
		yield {
			type: "result",
			subtype: "success",
			duration_ms: 1,
			duration_api_ms: 1,
			is_error: false,
			num_turns: 1,
			result: text,
			stop_reason: "end_turn",
			total_cost_usd: 0.003,
			usage: {
				input_tokens: 8,
				output_tokens: 4,
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
	const g = gen() as unknown as Query;
	(g as unknown as { interrupt?: () => Promise<void> }).interrupt = async () => {};
	return g;
}

describe("ExecutiveLoop egress wiring", () => {
	test("emits cloud_egress.turn after a successful dispatch when consent granted", async () => {
		// Grant consent.
		await sql`
			INSERT INTO consent_ledger (policy, enabled, granted_by, reason)
			VALUES ('autonomous_cloud_egress', true, 'user', 'test')
		`;
		const goal = await goals.create({
			title: "egress goal",
			description: "",
			origin: "owner",
			effectiveTrust: "owner",
			actor: "user",
			ownerPriority: 50,
		});
		const taskId = newGoalTaskId();
		const plan: readonly PlanStep[] = [{ taskId, body: "step", dependsOn: [] }];

		// First turn: plan.
		const loop1 = new ExecutiveLoop({
			bus,
			goals,
			lease,
			memoryServer: stubMemoryServer,
			subagents: stubSubagents,
			queryFn: () => successGenerator("ok"),
			planner: async () => plan,
			sql,
			userModel: stubUserModel,
		});
		await loop1.executeOneTurn({ runnerId: newGoalRunnerId() });
		await bus.flush();

		// Second turn: execute.
		const loop2 = new ExecutiveLoop({
			bus,
			goals,
			lease,
			memoryServer: stubMemoryServer,
			subagents: stubSubagents,
			queryFn: () => successGenerator("done"),
			sql,
			userModel: stubUserModel,
		});
		await loop2.executeOneTurn({ runnerId: newGoalRunnerId() });
		await bus.flush();

		void goal;
		const audit = await sql<Record<string, unknown>[]>`
			SELECT data FROM events WHERE type = 'cloud_egress.turn'
		`;
		expect(audit.length).toBe(1);
		const data = (audit[0]?.["data"] ?? {}) as Record<string, unknown>;
		expect(data["turnClass"]).toBe("executive");
		expect(data["dimensionsIncluded"]).toEqual(["communication_style"]);
	});

	test("blocks the task with consent_denied when autonomous egress is not granted", async () => {
		// No consent row inserted — default is false.
		await goals.create({
			title: "denied",
			description: "",
			origin: "owner",
			effectiveTrust: "owner",
			actor: "user",
			ownerPriority: 50,
		});
		const plan: readonly PlanStep[] = [{ taskId: newGoalTaskId(), body: "step", dependsOn: [] }];

		const loop1 = new ExecutiveLoop({
			bus,
			goals,
			lease,
			memoryServer: stubMemoryServer,
			subagents: stubSubagents,
			queryFn: () => successGenerator("ok"),
			planner: async () => plan,
			sql,
			userModel: stubUserModel,
		});
		await loop1.executeOneTurn({ runnerId: newGoalRunnerId() });
		await bus.flush();

		const loop2 = new ExecutiveLoop({
			bus,
			goals,
			lease,
			memoryServer: stubMemoryServer,
			subagents: stubSubagents,
			queryFn: () => successGenerator("would not reach here"),
			sql,
			userModel: stubUserModel,
		});
		await loop2.executeOneTurn({ runnerId: newGoalRunnerId() });
		await bus.flush();

		const failures = await sql<Record<string, unknown>[]>`
			SELECT data FROM events WHERE type = 'goal.task_failed'
		`;
		expect(failures.length).toBe(1);
		const data = (failures[0]?.["data"] ?? {}) as Record<string, unknown>;
		expect(data["message"]).toBe("consent_denied");
		// No cloud_egress.turn emitted.
		const egress = await sql<{ count: string }[]>`
			SELECT COUNT(*)::text AS count FROM events WHERE type = 'cloud_egress.turn'
		`;
		expect(Number(egress[0]?.count ?? "0")).toBe(0);
	});
});
