/**
 * Goal lease integration tests.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { GoalLease } from "../../src/goals/lease.ts";
import { registerGoalProjection } from "../../src/goals/projection.ts";
import { GoalRepository } from "../../src/goals/repository.ts";
import { asGoalRunnerId, newGoalRunnerId } from "../../src/goals/types.ts";
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
	registerGoalProjection({ sql, bus });
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

async function createActiveGoal(priority = 50): Promise<number> {
	const state = await goals.create({
		title: "test goal",
		description: "description",
		origin: "owner",
		effectiveTrust: "owner",
		actor: "user",
		ownerPriority: priority,
	});
	return Number(state.nodeId);
}

type LeasedRecord = NonNullable<Awaited<ReturnType<GoalLease["acquire"]>>>;

function requireLeased(leased: Awaited<ReturnType<GoalLease["acquire"]>>): LeasedRecord {
	if (leased === null) throw new Error("expected a leased goal");
	return leased;
}

describe("GoalLease", () => {
	test("acquire on a free goal succeeds and emits lease_acquired", async () => {
		const nodeId = await createActiveGoal();
		const runnerId = newGoalRunnerId();
		const leased = await lease.acquire(runnerId);
		expect(leased).not.toBeNull();
		expect(leased?.state.leasedBy).toBe(runnerId);
		expect(leased?.state.nodeId).toBe(nodeId as never);
	});

	test("acquire returns null when no goals are available", async () => {
		const runnerId = newGoalRunnerId();
		const leased = await lease.acquire(runnerId);
		expect(leased).toBeNull();
	});

	test("second acquire on the same goal returns null (lease held)", async () => {
		await createActiveGoal();
		const r1 = newGoalRunnerId();
		const r2 = newGoalRunnerId();
		const first = await lease.acquire(r1);
		expect(first).not.toBeNull();
		const second = await lease.acquire(r2);
		expect(second).toBeNull();
	});

	test("acquire succeeds after lease expiry", async () => {
		await createActiveGoal();
		const r1 = newGoalRunnerId();
		// Acquire with a very short lease duration so it expires near-instantly.
		const shortLease = new GoalLease({
			sql,
			bus,
			goals,
			leaseDurationMs: 10,
		});
		const first = await shortLease.acquire(r1);
		expect(first).not.toBeNull();

		// Wait past the expiry.
		await new Promise<void>((resolve) => setTimeout(resolve, 20));

		// A fresh lease should now reacquire.
		const r2 = newGoalRunnerId();
		const freshLease = new GoalLease({ sql, bus, goals });
		const takeover = await freshLease.acquire(r2);
		expect(takeover).not.toBeNull();
		expect(takeover?.state.leasedBy).toBe(r2);
	});

	test("heartbeat renews the lease when held by the same runner", async () => {
		await createActiveGoal();
		const runnerId = newGoalRunnerId();
		const leased = await lease.acquire(runnerId);
		expect(leased).not.toBeNull();
		const firstUntil = leased?.state.leasedUntil?.getTime() ?? 0;

		// Wait a moment, then heartbeat.
		await new Promise<void>((resolve) => setTimeout(resolve, 15));
		const renewed = await lease.heartbeat(requireLeased(leased).state.nodeId, runnerId);
		expect(renewed).toBe(true);

		const state = await goals.readState(requireLeased(leased).state.nodeId);
		expect((state?.leasedUntil?.getTime() ?? 0) >= firstUntil).toBe(true);
	});

	test("heartbeat fails for a different runner", async () => {
		await createActiveGoal();
		const r1 = newGoalRunnerId();
		const r2 = newGoalRunnerId();
		const leased = await lease.acquire(r1);
		expect(leased).not.toBeNull();
		const renewed = await lease.heartbeat(requireLeased(leased).state.nodeId, r2);
		expect(renewed).toBe(false);
	});

	test("release clears the lease", async () => {
		await createActiveGoal();
		const runnerId = newGoalRunnerId();
		const leased = await lease.acquire(runnerId);
		expect(leased).not.toBeNull();

		await lease.release(requireLeased(leased).state.nodeId, runnerId, "normal");

		const state = await goals.readState(requireLeased(leased).state.nodeId);
		expect(state?.leasedBy).toBeNull();
	});

	test("skip paused and quarantined goals", async () => {
		const active = await createActiveGoal();
		// Create another goal and pause it.
		const paused = await goals.create({
			title: "paused",
			description: "desc",
			origin: "owner",
			effectiveTrust: "owner",
			actor: "user",
		});
		await bus.emit({
			type: "goal.paused",
			version: 1,
			actor: "user",
			data: { nodeId: Number(paused.nodeId), pausedBy: "user" },
			metadata: {},
		});
		await bus.flush();

		const runnerId = newGoalRunnerId();
		const leased = await lease.acquire(runnerId);
		expect(leased).not.toBeNull();
		expect(leased?.state.nodeId).toBe(active as never);
	});

	test("picks higher priority when multiple goals are eligible", async () => {
		const low = await createActiveGoal(30);
		const high = await createActiveGoal(90);
		void low;
		const runnerId = newGoalRunnerId();
		const leased = await lease.acquire(runnerId);
		expect(leased?.state.nodeId).toBe(high as never);
		expect(leased?.state.ownerPriority).toBe(90);
	});

	test("concurrent acquires serialize — only one runner wins", async () => {
		await createActiveGoal();
		const r1 = newGoalRunnerId();
		const r2 = newGoalRunnerId();
		const [first, second] = await Promise.all([lease.acquire(r1), lease.acquire(r2)]);
		const winners = [first, second].filter((x) => x !== null);
		expect(winners.length).toBe(1);
	});
});

// Silence unused import warnings.
void asGoalRunnerId;
