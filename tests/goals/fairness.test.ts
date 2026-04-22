/**
 * Priority aging / fairness tests.
 *
 * Aging term: `time_since_last_worked / 1 week * agingBonus` is added to
 * the effective priority at lease-acquisition time.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { GoalLease } from "../../src/goals/lease.ts";
import { registerGoalProjection } from "../../src/goals/projection.ts";
import { GoalRepository } from "../../src/goals/repository.ts";
import { newGoalRunnerId } from "../../src/goals/types.ts";
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
	registerGoalProjection({ sql, bus });
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

describe("priority aging", () => {
	test("stale low-priority goal outranks fresh higher-priority after enough time", async () => {
		// Create a fresh priority=50 goal.
		const fresh = await goals.create({
			title: "fresh",
			description: "new",
			origin: "owner",
			effectiveTrust: "owner",
			actor: "user",
			ownerPriority: 50,
		});
		// Set its last_worked_at to NOW (just worked).
		await sql`
			UPDATE goal_state SET last_worked_at = now() WHERE node_id = ${Number(fresh.nodeId)}
		`;

		// Create a stale priority=30 goal, last worked 6 weeks ago.
		const stale = await goals.create({
			title: "stale",
			description: "old",
			origin: "owner",
			effectiveTrust: "owner",
			actor: "user",
			ownerPriority: 30,
		});
		await sql`
			UPDATE goal_state
			SET last_worked_at = now() - interval '6 weeks'
			WHERE node_id = ${Number(stale.nodeId)}
		`;

		// With 10pt/week aging: stale effective = 30 + 60 = 90 > fresh 50.
		const lease = new GoalLease({ sql, bus, goals });
		const leased = await lease.acquire(newGoalRunnerId());
		expect(leased?.state.nodeId).toBe(stale.nodeId);
	});

	test("manual priority override promotes a goal immediately", async () => {
		const a = await goals.create({
			title: "a",
			description: "a",
			origin: "owner",
			effectiveTrust: "owner",
			actor: "user",
			ownerPriority: 50,
		});
		const b = await goals.create({
			title: "b",
			description: "b",
			origin: "owner",
			effectiveTrust: "owner",
			actor: "user",
			ownerPriority: 50,
		});

		// Bump b's priority to 90.
		await bus.emit({
			type: "goal.priority_changed",
			version: 1,
			actor: "user",
			data: {
				nodeId: Number(b.nodeId),
				oldPriority: 50,
				newPriority: 90,
				reason: "boost",
			},
			metadata: {},
		});
		await bus.flush();
		void a;

		const lease = new GoalLease({ sql, bus, goals });
		const leased = await lease.acquire(newGoalRunnerId());
		expect(leased?.state.nodeId).toBe(b.nodeId);
	});
});
