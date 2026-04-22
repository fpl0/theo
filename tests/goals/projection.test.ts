/**
 * Goal projection integration tests.
 *
 * Every projection rule in `src/goals/projection.ts` has a corresponding
 * assertion here. The replay-rebuild test is the safety net: it tears the
 * projection down, replays every `goal.*` event in the log, and confirms
 * the rebuilt rows match the live projection row-by-row.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { newEventId } from "../../src/events/ids.ts";
import type { Event, EventOfType } from "../../src/events/types.ts";
import { registerGoalProjection } from "../../src/goals/projection.ts";
import { GoalRepository } from "../../src/goals/repository.ts";
import {
	asGoalRunnerId,
	asGoalTaskId,
	asGoalTurnId,
	newGoalTaskId,
	newGoalTurnId,
} from "../../src/goals/types.ts";
import { EdgeRepository } from "../../src/memory/graph/edges.ts";
import { NodeRepository } from "../../src/memory/graph/nodes.ts";
import { asNodeId, type NodeId } from "../../src/memory/graph/types.ts";
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
let edges: EdgeRepository;
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
	edges = new EdgeRepository(sql, bus);
	void edges;
	goals = new GoalRepository(sql, bus, nodes);
	registerGoalProjection({ sql, bus });
	await bus.start();
});

afterEach(async () => {
	try {
		await bus.flush();
	} catch {
		// Ignore flush failures in teardown.
	}
	try {
		await bus.stop();
	} catch {
		// Ignore stop failures on already-stopped buses.
	}
});

async function createTestGoal(origin: "owner" | "ideation" = "owner"): Promise<NodeId> {
	const state = await goals.create({
		title: `test goal ${origin}`,
		description: "test description",
		origin,
		effectiveTrust: origin === "owner" ? "owner" : "inferred",
		actor: origin === "owner" ? "user" : "system",
	});
	return state.nodeId;
}

describe("goal_state projection", () => {
	test("owner goal starts active", async () => {
		const nodeId = await createTestGoal("owner");
		const state = await goals.readState(nodeId);
		expect(state?.status).toBe("active");
		expect(state?.origin).toBe("owner");
		expect(state?.ownerPriority).toBe(50);
	});

	test("ideation proposal starts proposed", async () => {
		const nodeId = await createTestGoal("ideation");
		const state = await goals.readState(nodeId);
		expect(state?.status).toBe("proposed");
		expect(state?.origin).toBe("ideation");
	});

	test("confirm proposal → active", async () => {
		const nodeId = await createTestGoal("ideation");
		await bus.emit({
			type: "goal.confirmed",
			version: 1,
			actor: "user",
			data: { nodeId: Number(nodeId), confirmedBy: "user" },
			metadata: {},
		});
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.status).toBe("active");
	});

	test("priority_changed updates owner_priority", async () => {
		const nodeId = await createTestGoal();
		await bus.emit({
			type: "goal.priority_changed",
			version: 1,
			actor: "user",
			data: { nodeId: Number(nodeId), oldPriority: 50, newPriority: 75, reason: "test" },
			metadata: {},
		});
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.ownerPriority).toBe(75);
	});

	test("plan_updated v1 inserts tasks, plan snapshot stored, current_task_id cleared", async () => {
		const nodeId = await createTestGoal();
		const taskA = newGoalTaskId();
		const taskB = newGoalTaskId();
		const plan = [
			{ taskId: taskA, body: "first", dependsOn: [] },
			{ taskId: taskB, body: "second", dependsOn: [taskA] },
		];
		await bus.emit({
			type: "goal.plan_updated",
			version: 1,
			actor: "theo",
			data: {
				nodeId: Number(nodeId),
				planVersion: 1,
				plan,
				reason: "initial_plan",
				previousPlanHash: null,
			},
			metadata: {},
		});
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.planVersion).toBe(1);
		expect(state?.plan.length).toBe(2);
		expect(state?.currentTaskId).toBeNull();
		const tasks = await goals.tasks(nodeId);
		expect(tasks.length).toBe(2);
		expect(tasks[0]?.status).toBe("pending");
	});

	test("plan_updated v2 abandons previous-version tasks, inserts new", async () => {
		const nodeId = await createTestGoal();
		const taskA = newGoalTaskId();
		const taskB = newGoalTaskId();
		await bus.emit({
			type: "goal.plan_updated",
			version: 1,
			actor: "theo",
			data: {
				nodeId: Number(nodeId),
				planVersion: 1,
				plan: [{ taskId: taskA, body: "first", dependsOn: [] }],
				reason: "initial",
				previousPlanHash: null,
			},
			metadata: {},
		});
		await bus.flush();

		await bus.emit({
			type: "goal.plan_updated",
			version: 1,
			actor: "theo",
			data: {
				nodeId: Number(nodeId),
				planVersion: 2,
				plan: [{ taskId: taskB, body: "revised", dependsOn: [] }],
				reason: "replan",
				previousPlanHash: "deadbeef",
			},
			metadata: {},
		});
		await bus.flush();

		const tasks = await goals.tasks(nodeId);
		const a = tasks.find((t) => t.taskId === taskA);
		const b = tasks.find((t) => t.taskId === taskB);
		expect(a?.status).toBe("abandoned");
		expect(b?.status).toBe("pending");
		const state = await goals.readState(nodeId);
		expect(state?.planVersion).toBe(2);
	});

	test("task_started sets current_task_id, last_worked_at updated", async () => {
		const nodeId = await createTestGoal();
		const taskId = newGoalTaskId();
		await emitPlan(bus, nodeId, [{ taskId, body: "do it", dependsOn: [] }]);
		const turnId = newGoalTurnId();
		const runnerId = asGoalRunnerId("runner-1");
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
				maxTurns: 10,
				maxBudgetUsd: 0.1,
				maxDurationMs: 60_000,
			},
			metadata: {},
		});
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.currentTaskId).toBe(taskId);
		expect(state?.lastWorkedAt).not.toBeNull();
		const task = await goals.getTask(taskId);
		expect(task?.status).toBe("in_progress");
	});

	test("task_completed clears current_task_id, resets consecutive_failures", async () => {
		const nodeId = await createTestGoal();
		const taskId = newGoalTaskId();
		await emitPlan(bus, nodeId, [{ taskId, body: "do it", dependsOn: [] }]);
		const turnId = newGoalTurnId();
		const runnerId = asGoalRunnerId("runner-1");
		await emitTaskStarted(bus, nodeId, taskId, turnId, runnerId);
		// Simulate a prior failure so consecutive_failures > 0
		await sql`UPDATE goal_state SET consecutive_failures = 2 WHERE node_id = ${Number(nodeId)}`;

		await bus.emit({
			type: "goal.task_completed",
			version: 1,
			actor: "theo",
			data: {
				nodeId: Number(nodeId),
				taskId,
				turnId,
				outcome: "done",
				artifactIds: [],
				totalTokens: 100,
				totalCostUsd: 0.01,
			},
			metadata: {},
		});
		await bus.flush();

		const state = await goals.readState(nodeId);
		expect(state?.currentTaskId).toBeNull();
		expect(state?.consecutiveFailures).toBe(0);
		const task = await goals.getTask(taskId);
		expect(task?.status).toBe("completed");
	});

	test("task_failed increments consecutive_failures, failure_count", async () => {
		const nodeId = await createTestGoal();
		const taskId = newGoalTaskId();
		await emitPlan(bus, nodeId, [{ taskId, body: "do it", dependsOn: [] }]);
		const turnId = newGoalTurnId();
		await emitTaskStarted(bus, nodeId, taskId, turnId, asGoalRunnerId("r1"));

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
		const state = await goals.readState(nodeId);
		expect(state?.consecutiveFailures).toBe(1);
		const task = await goals.getTask(taskId);
		expect(task?.failureCount).toBe(1);
		expect(task?.status).toBe("failed");
	});

	test("task_yielded clears current_task_id, increments yield_count", async () => {
		const nodeId = await createTestGoal();
		const taskId = newGoalTaskId();
		await emitPlan(bus, nodeId, [{ taskId, body: "do it", dependsOn: [] }]);
		const turnId = newGoalTurnId();
		await emitTaskStarted(bus, nodeId, taskId, turnId, asGoalRunnerId("r1"));

		await bus.emit({
			type: "goal.task_yielded",
			version: 1,
			actor: "theo",
			data: {
				nodeId: Number(nodeId),
				taskId,
				turnId,
				resumeKey: null,
				reason: "preempted",
			},
			metadata: {},
		});
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.currentTaskId).toBeNull();
		const task = await goals.getTask(taskId);
		expect(task?.yieldCount).toBe(1);
		expect(task?.status).toBe("yielded");
	});

	test("blocked → unblocked status toggle", async () => {
		const nodeId = await createTestGoal();
		const taskId = newGoalTaskId();
		await emitPlan(bus, nodeId, [{ taskId, body: "do it", dependsOn: [] }]);
		await bus.emit({
			type: "goal.blocked",
			version: 1,
			actor: "system",
			data: {
				nodeId: Number(nodeId),
				taskId,
				blocker: { kind: "user_input", question: "need answer" },
			},
			metadata: {},
		});
		await bus.flush();
		expect((await goals.readState(nodeId))?.status).toBe("blocked");
		expect((await goals.readState(nodeId))?.blockedReason?.kind).toBe("user_input");

		await bus.emit({
			type: "goal.unblocked",
			version: 1,
			actor: "user",
			data: { nodeId: Number(nodeId), taskId, unblockedBy: "user" },
			metadata: {},
		});
		await bus.flush();
		expect((await goals.readState(nodeId))?.status).toBe("active");
		expect((await goals.readState(nodeId))?.blockedReason).toBeNull();
	});

	test("pause → resume toggles status", async () => {
		const nodeId = await createTestGoal();
		await bus.emit({
			type: "goal.paused",
			version: 1,
			actor: "user",
			data: { nodeId: Number(nodeId), pausedBy: "user" },
			metadata: {},
		});
		await bus.flush();
		expect((await goals.readState(nodeId))?.status).toBe("paused");

		await bus.emit({
			type: "goal.resumed",
			version: 1,
			actor: "user",
			data: { nodeId: Number(nodeId), resumedBy: "user" },
			metadata: {},
		});
		await bus.flush();
		expect((await goals.readState(nodeId))?.status).toBe("active");
	});

	test("cancelled is terminal — subsequent events do not change state", async () => {
		const nodeId = await createTestGoal();
		await bus.emit({
			type: "goal.cancelled",
			version: 1,
			actor: "user",
			data: { nodeId: Number(nodeId), cancelledBy: "user", reason: "owner_cancelled" },
			metadata: {},
		});
		await bus.flush();
		expect((await goals.readState(nodeId))?.status).toBe("cancelled");

		// Attempt resume — should NOT transition back to active.
		await bus.emit({
			type: "goal.resumed",
			version: 1,
			actor: "user",
			data: { nodeId: Number(nodeId), resumedBy: "user" },
			metadata: {},
		});
		await bus.flush();
		expect((await goals.readState(nodeId))?.status).toBe("cancelled");
	});

	test("redaction sets redacted flag and masks task bodies when requested", async () => {
		const nodeId = await createTestGoal();
		const taskId = newGoalTaskId();
		await emitPlan(bus, nodeId, [{ taskId, body: "secret task", dependsOn: [] }]);

		await bus.emit({
			type: "goal.redacted",
			version: 1,
			actor: "user",
			data: {
				nodeId: Number(nodeId),
				redactedFields: ["title", "description", "task_bodies"],
				redactedBy: "user",
			},
			metadata: {},
		});
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.redacted).toBe(true);
		const tasks = await goals.tasks(nodeId);
		expect(tasks[0]?.redacted).toBe(true);
	});

	test("expired transitions proposed to expired", async () => {
		const nodeId = await createTestGoal("ideation");
		await bus.emit({
			type: "goal.expired",
			version: 1,
			actor: "system",
			data: { nodeId: Number(nodeId) },
			metadata: {},
		});
		await bus.flush();
		expect((await goals.readState(nodeId))?.status).toBe("expired");
	});

	test("double task_started with same turnId is idempotent", async () => {
		const nodeId = await createTestGoal();
		const taskId = newGoalTaskId();
		await emitPlan(bus, nodeId, [{ taskId, body: "do it", dependsOn: [] }]);
		const turnId = newGoalTurnId();
		await emitTaskStarted(bus, nodeId, taskId, turnId, asGoalRunnerId("r1"));
		const firstTask = await goals.getTask(taskId);
		// Emit the same event again (different event id but same turnId).
		await emitTaskStarted(bus, nodeId, taskId, turnId, asGoalRunnerId("r1"));
		const secondTask = await goals.getTask(taskId);
		expect(firstTask?.startedAt?.getTime()).toBe(secondTask?.startedAt?.getTime());
	});
});

describe("replay rebuild", () => {
	test("projection rebuilt from zero matches live state", async () => {
		const nodeId = await createTestGoal();
		const taskId = newGoalTaskId();
		await emitPlan(bus, nodeId, [{ taskId, body: "work", dependsOn: [] }]);
		const turnId = newGoalTurnId();
		await emitTaskStarted(bus, nodeId, taskId, turnId, asGoalRunnerId("r1"));
		await bus.emit({
			type: "goal.task_completed",
			version: 1,
			actor: "theo",
			data: {
				nodeId: Number(nodeId),
				taskId,
				turnId,
				outcome: "done",
				artifactIds: [],
				totalTokens: 42,
				totalCostUsd: 0.02,
			},
			metadata: {},
		});
		await bus.flush();

		// Snapshot live state.
		const liveState = await goals.readState(nodeId);
		const liveTasks = await goals.tasks(nodeId);

		// Stop the bus, truncate the projection + cursors, rebuild from
		// events by starting a fresh bus + handler (replay re-runs decision
		// handlers). Clearing cursors is essential — otherwise replay starts
		// from "already processed" and sees no events.
		await bus.stop();
		await sql`TRUNCATE goal_state, goal_task CASCADE`;
		await sql`DELETE FROM handler_cursors`;

		const replayBus = createTestBus(sql);
		const replayGoals = new GoalRepository(sql, replayBus, nodes);
		registerGoalProjection({ sql, bus: replayBus });
		await replayBus.start();
		await replayBus.flush();

		const replayedState = await replayGoals.readState(nodeId);
		const replayedTasks = await replayGoals.tasks(nodeId);

		expect(replayedState?.status).toBe(liveState?.status);
		expect(replayedState?.planVersion).toBe(liveState?.planVersion);
		expect(replayedState?.consecutiveFailures).toBe(liveState?.consecutiveFailures);
		expect(replayedTasks.length).toBe(liveTasks.length);
		expect(replayedTasks[0]?.status).toBe(liveTasks[0]?.status);

		await replayBus.stop();
		// Put bus back so afterEach doesn't trip on a stopped one.
		bus = replayBus;
	});
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function emitPlan(
	bus: EventBus,
	nodeId: NodeId,
	plan: { taskId: string; body: string; dependsOn: string[] }[],
): Promise<void> {
	await bus.emit({
		type: "goal.plan_updated",
		version: 1,
		actor: "theo",
		data: {
			nodeId: Number(nodeId),
			planVersion: 1,
			plan: plan.map((p) => ({
				taskId: asGoalTaskId(p.taskId),
				body: p.body,
				dependsOn: p.dependsOn.map(asGoalTaskId),
			})),
			reason: "test_plan",
			previousPlanHash: null,
		},
		metadata: {},
	});
	await bus.flush();
}

async function emitTaskStarted(
	bus: EventBus,
	nodeId: NodeId,
	taskId: ReturnType<typeof newGoalTaskId>,
	turnId: ReturnType<typeof newGoalTurnId>,
	runnerId: ReturnType<typeof asGoalRunnerId>,
): Promise<void> {
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
			maxTurns: 10,
			maxBudgetUsd: 0.1,
			maxDurationMs: 60_000,
		},
		metadata: {},
	});
	await bus.flush();
}

// Keep the unused imports out of tsc's complaint list while the tests use
// them implicitly through helpers.
void newEventId;
void asGoalTurnId;
void asNodeId;
// Cast a sample event just to ensure the type imports stay live.
const _sample: EventOfType<"goal.created"> | null = null;
void _sample;
const _evt: Event | null = null;
void _evt;
