/**
 * Recovery tests — dangling task abandonment at startup.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { registerGoalHandlers } from "../../src/goals/handlers.ts";
import { registerGoalProjection } from "../../src/goals/projection.ts";
import { runRecovery } from "../../src/goals/recovery.ts";
import { GoalRepository } from "../../src/goals/repository.ts";
import {
	asGoalRunnerId,
	newGoalRunnerId,
	newGoalTaskId,
	newGoalTurnId,
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
	// Full handler registration so the abandonment + poison breaker work together.
	registerGoalProjection({ sql, bus });
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

async function createActiveGoalWithInProgressTask(
	runnerId: ReturnType<typeof newGoalRunnerId>,
): Promise<{
	nodeId: import("../../src/memory/graph/types.ts").NodeId;
	taskId: ReturnType<typeof newGoalTaskId>;
	turnId: ReturnType<typeof newGoalTurnId>;
}> {
	const state = await goals.create({
		title: "goal",
		description: "desc",
		origin: "owner",
		effectiveTrust: "owner",
		actor: "user",
	});
	const taskId = newGoalTaskId();
	const turnId = newGoalTurnId();
	await bus.emit({
		type: "goal.plan_updated",
		version: 1,
		actor: "theo",
		data: {
			nodeId: Number(state.nodeId),
			planVersion: 1,
			plan: [{ taskId, body: "do it", dependsOn: [] }],
			reason: "initial",
			previousPlanHash: null,
		},
		metadata: {},
	});
	await bus.flush();
	await bus.emit({
		type: "goal.task_started",
		version: 1,
		actor: "theo",
		data: {
			nodeId: Number(state.nodeId),
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
	return { nodeId: state.nodeId, taskId, turnId };
}

describe("recovery", () => {
	test("dangling task → goal.task_abandoned synthesized", async () => {
		const deadRunner = asGoalRunnerId("dead-runner");
		await createActiveGoalWithInProgressTask(deadRunner);

		const currentRunner = newGoalRunnerId();
		const result = await runRecovery({ bus, goals }, currentRunner);
		await bus.flush();
		expect(result.abandonedTasks).toBe(1);
	});

	test("multiple dangling tasks all abandoned", async () => {
		const deadRunner = asGoalRunnerId("runner-A");
		for (let i = 0; i < 3; i++) {
			await createActiveGoalWithInProgressTask(deadRunner);
		}
		const currentRunner = newGoalRunnerId();
		const result = await runRecovery({ bus, goals }, currentRunner);
		await bus.flush();
		expect(result.abandonedTasks).toBe(3);
	});

	test("clean restart (no dangling) — no abandonment emitted", async () => {
		await goals.create({
			title: "clean",
			description: "desc",
			origin: "owner",
			effectiveTrust: "owner",
			actor: "user",
		});
		const result = await runRecovery({ bus, goals }, newGoalRunnerId());
		expect(result.abandonedTasks).toBe(0);
	});

	test("abandonment counts as failure — consecutive_failures increments", async () => {
		const deadRunner = asGoalRunnerId("dead");
		const { nodeId } = await createActiveGoalWithInProgressTask(deadRunner);
		const currentRunner = newGoalRunnerId();
		await runRecovery({ bus, goals }, currentRunner);
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.consecutiveFailures).toBeGreaterThanOrEqual(1);
	});

	test("stale leases are released on recovery", async () => {
		// Create a leased goal with an expired lease.
		const state = await goals.create({
			title: "stale",
			description: "desc",
			origin: "owner",
			effectiveTrust: "owner",
			actor: "user",
		});
		await sql`
			UPDATE goal_state
			SET leased_by = 'stale-runner',
			    leased_until = now() - interval '10 minutes'
			WHERE node_id = ${Number(state.nodeId)}
		`;
		const result = await runRecovery({ bus, goals }, newGoalRunnerId());
		await bus.flush();
		expect(result.releasedLeases).toBe(1);
		const after = await goals.readState(state.nodeId);
		expect(after?.leasedBy).toBeNull();
	});
});
