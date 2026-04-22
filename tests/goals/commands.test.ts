/**
 * Operator command handler tests.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import {
	auditGoal,
	cancelGoal,
	isCliOnlyCommand,
	pauseGoal,
	promoteGoal,
	redactGoal,
	resumeGoal,
	setAutonomy,
	setPriority,
} from "../../src/goals/commands.ts";
import { registerGoalHandlers } from "../../src/goals/handlers.ts";
import { GoalRepository } from "../../src/goals/repository.ts";
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

async function createOwnerGoal(): Promise<NodeId> {
	const state = await goals.create({
		title: "test",
		description: "desc",
		origin: "owner",
		effectiveTrust: "owner",
		actor: "user",
	});
	return state.nodeId;
}

async function createProposedGoal(): Promise<NodeId> {
	const state = await goals.create({
		title: "proposal",
		description: "desc",
		origin: "ideation",
		effectiveTrust: "inferred",
		actor: "system",
	});
	return state.nodeId;
}

describe("commands", () => {
	test("pauseGoal emits goal.paused", async () => {
		const nodeId = await createOwnerGoal();
		const result = await pauseGoal({ bus, goals }, nodeId, "user");
		expect(result.ok).toBe(true);
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.status).toBe("paused");
	});

	test("pauseGoal is idempotent when already paused", async () => {
		const nodeId = await createOwnerGoal();
		await pauseGoal({ bus, goals }, nodeId, "user");
		await bus.flush();
		const result = await pauseGoal({ bus, goals }, nodeId, "user");
		expect(result.ok).toBe(true); // no new event, but still ok
	});

	test("pauseGoal on non-existent returns not_found", async () => {
		const result = await pauseGoal({ bus, goals }, 9999 as unknown as NodeId, "user");
		expect(result.ok).toBe(false);
		if (!result.ok) expect(result.error.code).toBe("not_found");
	});

	test("cancelGoal emits goal.cancelled", async () => {
		const nodeId = await createOwnerGoal();
		const result = await cancelGoal({ bus, goals }, nodeId, "user");
		expect(result.ok).toBe(true);
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.status).toBe("cancelled");
	});

	test("resumeGoal from paused → active", async () => {
		const nodeId = await createOwnerGoal();
		await pauseGoal({ bus, goals }, nodeId, "user");
		await bus.flush();
		const resumed = await resumeGoal({ bus, goals }, nodeId, "user");
		expect(resumed.ok).toBe(true);
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.status).toBe("active");
	});

	test("promoteGoal on proposed emits goal.confirmed", async () => {
		const nodeId = await createProposedGoal();
		const result = await promoteGoal({ bus, goals }, nodeId, "user");
		expect(result.ok).toBe(true);
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.status).toBe("active");
	});

	test("setPriority emits goal.priority_changed", async () => {
		const nodeId = await createOwnerGoal();
		const result = await setPriority({ bus, goals }, nodeId, 80, "user");
		expect(result.ok).toBe(true);
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.ownerPriority).toBe(80);
	});

	test("setPriority rejects out-of-range", async () => {
		const nodeId = await createOwnerGoal();
		const result = await setPriority({ bus, goals }, nodeId, 200, "user");
		expect(result.ok).toBe(false);
		if (!result.ok) expect(result.error.code).toBe("invalid_argument");
	});

	test("redactGoal on CLI succeeds", async () => {
		const nodeId = await createOwnerGoal();
		const result = await redactGoal(
			{ bus, goals },
			nodeId,
			"user",
			["title", "description"],
			"cli",
		);
		expect(result.ok).toBe(true);
		await bus.flush();
		const state = await goals.readState(nodeId);
		expect(state?.redacted).toBe(true);
	});

	test("redactGoal from Telegram is forbidden", async () => {
		const nodeId = await createOwnerGoal();
		const result = await redactGoal({ bus, goals }, nodeId, "user", ["title"], "telegram");
		expect(result.ok).toBe(false);
		if (!result.ok) expect(result.error.code).toBe("forbidden");
	});

	test("isCliOnlyCommand identifies /redact and /autonomy", () => {
		expect(isCliOnlyCommand("/redact")).toBe(true);
		expect(isCliOnlyCommand("/autonomy")).toBe(true);
		expect(isCliOnlyCommand("/pause")).toBe(false);
	});

	test("setAutonomy from CLI persists row", async () => {
		const result = await setAutonomy(
			{ bus, goals },
			"code.write.workspace",
			4,
			"user",
			"expand",
			"cli",
		);
		expect(result.ok).toBe(true);
		const policy = await goals.getAutonomyPolicy("code.write.workspace");
		expect(policy?.level).toBe(4);
	});

	test("setAutonomy from Telegram forbidden", async () => {
		const result = await setAutonomy(
			{ bus, goals },
			"code.write.workspace",
			5,
			"user",
			null,
			"telegram",
		);
		expect(result.ok).toBe(false);
	});

	test("auditGoal returns chronological event list", async () => {
		const nodeId = await createOwnerGoal();
		await setPriority({ bus, goals }, nodeId, 70, "user");
		await bus.flush();
		const result = await auditGoal({ bus, goals }, nodeId);
		expect(result.ok).toBe(true);
		if (result.ok) {
			const types = result.value.map((e) => e.type);
			expect(types).toContain("goal.created");
			expect(types).toContain("goal.priority_changed");
		}
	});
});
