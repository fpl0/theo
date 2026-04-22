/**
 * Poison goal circuit breaker tests.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import type { Event } from "../../src/events/types.ts";
import { registerGoalHandlers } from "../../src/goals/handlers.ts";
import { GoalLease } from "../../src/goals/lease.ts";
import { GoalRepository } from "../../src/goals/repository.ts";
import {
	asGoalRunnerId,
	newGoalRunnerId,
	newGoalTaskId,
	newGoalTurnId,
	POISON_THRESHOLD,
} from "../../src/goals/types.ts";
import { NodeRepository } from "../../src/memory/graph/nodes.ts";
import type { NodeId } from "../../src/memory/graph/types.ts";
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

async function createGoalWithPlan(): Promise<{
	nodeId: NodeId;
	taskId: ReturnType<typeof newGoalTaskId>;
}> {
	const state = await goals.create({
		title: "poison target",
		description: "desc",
		origin: "owner",
		effectiveTrust: "owner",
		actor: "user",
	});
	const taskId = newGoalTaskId();
	await bus.emit({
		type: "goal.plan_updated",
		version: 1,
		actor: "theo",
		data: {
			nodeId: Number(state.nodeId),
			planVersion: 1,
			plan: [{ taskId, body: "step", dependsOn: [] }],
			reason: "initial",
			previousPlanHash: null,
		},
		metadata: {},
	});
	await bus.flush();
	return { nodeId: state.nodeId, taskId };
}

async function emitFailure(
	nodeId: NodeId,
	taskId: ReturnType<typeof newGoalTaskId>,
): Promise<void> {
	const turnId = newGoalTurnId();
	const runnerId = asGoalRunnerId("r-poison");
	await bus.emit({
		type: "goal.task_started",
		version: 1,
		actor: "theo",
		data: {
			nodeId: Number(nodeId),
			taskId,
			turnId,
			runnerId,
			subagent: "planner",
			maxTurns: 5,
			maxBudgetUsd: 0.1,
			maxDurationMs: 30_000,
		},
		metadata: {},
	});
	await bus.flush();
	await bus.emit({
		type: "goal.task_failed",
		version: 1,
		actor: "theo",
		data: {
			nodeId: Number(nodeId),
			taskId,
			turnId,
			errorClass: "tool_error",
			message: "boom",
			recoverable: true,
		},
		metadata: {},
	});
	await bus.flush();
}

describe("poison circuit breaker", () => {
	test("single failure does not quarantine", async () => {
		const { nodeId, taskId } = await createGoalWithPlan();
		await emitFailure(nodeId, taskId);
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.status).not.toBe("quarantined");
		expect(state?.consecutiveFailures).toBe(1);
	});

	test("3 consecutive failures → quarantined", async () => {
		const { nodeId, taskId } = await createGoalWithPlan();
		for (let i = 0; i < POISON_THRESHOLD; i++) {
			await emitFailure(nodeId, taskId);
		}
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.status).toBe("quarantined");
		expect(state?.quarantinedReason).toContain("consecutive failures");
	});

	test("notification emitted on quarantine", async () => {
		const { nodeId, taskId } = await createGoalWithPlan();
		for (let i = 0; i < POISON_THRESHOLD; i++) {
			await emitFailure(nodeId, taskId);
		}
		await bus.flush();
		const events = await sql<Event[]>`
			SELECT type, data FROM events
			WHERE type = 'notification.created'
			ORDER BY id ASC
		`;
		const notif = events.find((e) => (e.data as { source?: string }).source === "goal-quarantine");
		expect(notif).not.toBeUndefined();
	});

	test("success after 2 failures resets the counter", async () => {
		const { nodeId, taskId } = await createGoalWithPlan();
		await emitFailure(nodeId, taskId);
		await emitFailure(nodeId, taskId);
		await bus.flush();
		let state = await goals.readState(nodeId);
		expect(state?.consecutiveFailures).toBe(2);

		// Emit a success.
		const turnId = newGoalTurnId();
		const runnerId = newGoalRunnerId();
		await bus.emit({
			type: "goal.task_started",
			version: 1,
			actor: "theo",
			data: {
				nodeId: Number(nodeId),
				taskId,
				turnId,
				runnerId,
				subagent: "planner",
				maxTurns: 5,
				maxBudgetUsd: 0.1,
				maxDurationMs: 30_000,
			},
			metadata: {},
		});
		await bus.flush();
		await bus.emit({
			type: "goal.task_completed",
			version: 1,
			actor: "theo",
			data: {
				nodeId: Number(nodeId),
				taskId,
				turnId,
				outcome: "recovered",
				artifactIds: [],
				totalTokens: 1,
				totalCostUsd: 0.01,
			},
			metadata: {},
		});
		await bus.flush();

		state = await goals.readState(nodeId);
		expect(state?.consecutiveFailures).toBe(0);
		expect(state?.status).not.toBe("quarantined");
	});

	test("quarantined goal is not picked by lease", async () => {
		const { nodeId, taskId } = await createGoalWithPlan();
		for (let i = 0; i < POISON_THRESHOLD; i++) {
			await emitFailure(nodeId, taskId);
		}
		await bus.flush();
		expect((await goals.readState(nodeId))?.status).toBe("quarantined");

		const lease = new GoalLease({ sql, bus, goals });
		const leased = await lease.acquire(newGoalRunnerId());
		expect(leased).toBeNull();
	});
});
