/**
 * Integration tests for forgetting curves.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import {
	applyForgettingCurves,
	BASE_HALF_LIFE_DAYS,
	computeDecayedImportance,
	IMPORTANCE_FLOOR,
} from "../../src/memory/forgetting.ts";
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

beforeAll(async () => {
	pool = createTestPool();
	const connectResult = await pool.connect();
	if (!connectResult.ok) {
		throw new Error(`Test setup failed: ${connectResult.error.message}`);
	}
	sql = pool.sql;
	bus = createTestBus(sql);
	await bus.start();
	nodes = new NodeRepository(sql, bus, createMockEmbeddings());
});

beforeEach(async () => {
	await sql`TRUNCATE node, edge CASCADE`;
	await cleanEventTables(sql);
});

afterAll(async () => {
	await bus.stop();
	if (pool) await pool.end();
});

describe("computeDecayedImportance", () => {
	test("basic decay: half life reduces importance by half", () => {
		const decayed = computeDecayedImportance(0.8, 0, BASE_HALF_LIFE_DAYS);
		expect(decayed).toBeCloseTo(0.4, 2);
	});

	test("access count extends half life", () => {
		const noAccess = computeDecayedImportance(0.8, 0, BASE_HALF_LIFE_DAYS);
		const tenAccess = computeDecayedImportance(0.8, 10, BASE_HALF_LIFE_DAYS);
		// tenAccess half-life is 2x, so after BASE_HALF_LIFE_DAYS it is 0.8 * 0.5^(1/2) ≈ 0.566
		expect(tenAccess).toBeGreaterThan(noAccess);
	});

	test("floor respected", () => {
		expect(computeDecayedImportance(0.06, 0, 365 * 5)).toBe(IMPORTANCE_FLOOR);
	});

	test("no time elapsed returns current importance", () => {
		expect(computeDecayedImportance(0.5, 0, 0)).toBe(0.5);
	});
});

describe("applyForgettingCurves (SQL pass)", () => {
	test("rewrites importance on eligible nodes", async () => {
		const node = await nodes.create({
			kind: "fact",
			body: "Decay candidate",
			actor: "user",
			importance: 0.8,
		});
		// Backdate last_accessed_at by >> half-life
		const pastDate = new Date(Date.now() - 365 * 86_400_000);
		await sql`UPDATE node SET last_accessed_at = ${pastDate}, access_count = 0 WHERE id = ${node.id}`;

		const count = await applyForgettingCurves({ sql, bus });
		expect(count).toBe(1);

		const rows = await sql`SELECT importance FROM node WHERE id = ${node.id}`;
		expect((rows[0]?.["importance"] as number) < 0.8).toBe(true);
	});

	test("pattern nodes exempt from decay", async () => {
		const pattern = await nodes.create({
			kind: "pattern",
			body: "Pattern survives",
			actor: "system",
			importance: 0.9,
		});
		await sql`UPDATE node SET last_accessed_at = ${new Date(0)} WHERE id = ${pattern.id}`;

		await applyForgettingCurves({ sql, bus });
		const rows = await sql`SELECT importance FROM node WHERE id = ${pattern.id}`;
		expect(rows[0]?.["importance"]).toBe(0.9);
	});

	test("principle nodes exempt from decay", async () => {
		const principle = await nodes.create({
			kind: "principle",
			body: "Principle survives",
			actor: "system",
			importance: 0.95,
		});
		await sql`UPDATE node SET last_accessed_at = ${new Date(0)} WHERE id = ${principle.id}`;

		await applyForgettingCurves({ sql, bus });
		const rows = await sql`SELECT importance FROM node WHERE id = ${principle.id}`;
		expect(rows[0]?.["importance"]).toBe(0.95);
	});

	test("emits memory.node.decayed when nodes decayed", async () => {
		await cleanEventTables(sql);
		const node = await nodes.create({
			kind: "fact",
			body: "Emit decay event",
			actor: "user",
			importance: 0.7,
		});
		const pastDate = new Date(Date.now() - 365 * 86_400_000);
		await sql`UPDATE node SET last_accessed_at = ${pastDate} WHERE id = ${node.id}`;
		await applyForgettingCurves({ sql, bus });
		await bus.flush();

		const rows = await sql`SELECT 1 FROM events WHERE type = 'memory.node.decayed'`;
		expect(rows.length).toBeGreaterThan(0);
	});

	test("floor enforced on database-level decay", async () => {
		const node = await nodes.create({
			kind: "fact",
			body: "Near floor",
			actor: "user",
			importance: 0.06,
		});
		const pastDate = new Date(Date.now() - 10 * 365 * 86_400_000);
		await sql`UPDATE node SET last_accessed_at = ${pastDate} WHERE id = ${node.id}`;

		await applyForgettingCurves({ sql, bus });
		const rows = await sql`SELECT importance FROM node WHERE id = ${node.id}`;
		expect(rows[0]?.["importance"]).toBe(IMPORTANCE_FLOOR);
	});
});
