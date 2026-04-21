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
	HALF_LIFE_DAYS,
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

	// Phase 13a: kind-specific half-lives
	test("preference kind decays slowly: half-life ~120 days", () => {
		const afterHalfLife = computeDecayedImportance(0.8, 0, HALF_LIFE_DAYS.preference, "preference");
		expect(afterHalfLife).toBeCloseTo(0.4, 2);
	});

	test("preference barely moves over 30 days", () => {
		// 30 / 120 = 0.25 half-lives -> factor 0.5^0.25 ~ 0.841
		const after30 = computeDecayedImportance(0.8, 0, 30, "preference");
		expect(after30).toBeCloseTo(0.8 * 0.5 ** 0.25, 3);
		expect(after30).toBeGreaterThan(0.65); // sanity: still high
	});

	test("observation kind decays fast: half-life 14 days", () => {
		const afterHalfLife = computeDecayedImportance(
			0.8,
			0,
			HALF_LIFE_DAYS.observation,
			"observation",
		);
		expect(afterHalfLife).toBeCloseTo(0.4, 2);
	});

	test("event kind decays fast: half-life 14 days", () => {
		const afterHalfLife = computeDecayedImportance(0.8, 0, HALF_LIFE_DAYS.event, "event");
		expect(afterHalfLife).toBeCloseTo(0.4, 2);
	});

	test("pattern kind never decays (Infinity half-life)", () => {
		expect(HALF_LIFE_DAYS.pattern).toBe(Number.POSITIVE_INFINITY);
		const decayed = computeDecayedImportance(0.9, 0, 365, "pattern");
		expect(decayed).toBe(0.9);
	});

	test("principle kind never decays (Infinity half-life)", () => {
		expect(HALF_LIFE_DAYS.principle).toBe(Number.POSITIVE_INFINITY);
		const decayed = computeDecayedImportance(0.95, 0, 365, "principle");
		expect(decayed).toBe(0.95);
	});

	test("default kind argument is 'fact' (backward-compat)", () => {
		expect(HALF_LIFE_DAYS.fact).toBe(BASE_HALF_LIFE_DAYS);
		const defaulted = computeDecayedImportance(0.8, 0, BASE_HALF_LIFE_DAYS);
		const explicit = computeDecayedImportance(0.8, 0, BASE_HALF_LIFE_DAYS, "fact");
		expect(defaulted).toBe(explicit);
	});
});

describe("applyForgettingCurves kind-specific decay (Phase 13a)", () => {
	test("preference node survives ~30 days mostly intact", async () => {
		const pref = await nodes.create({
			kind: "preference",
			body: "Prefers dark mode across every editor",
			actor: "user",
			importance: 0.8,
		});
		const past = new Date(Date.now() - 30 * 86_400_000);
		await sql`
			UPDATE node SET last_accessed_at = ${past}, access_count = 0 WHERE id = ${pref.id}
		`;
		await applyForgettingCurves({ sql, bus });

		const rows = await sql`SELECT importance FROM node WHERE id = ${pref.id}`;
		const after = rows[0]?.["importance"] as number;
		// Preferences have a 120-day half-life; after 30 days importance
		// should remain > 0.65 (roughly 0.8 * 0.5^0.25 ≈ 0.673).
		expect(after).toBeGreaterThan(0.6);
	});

	test("observation node halves after ~14 days", async () => {
		const obs = await nodes.create({
			kind: "observation",
			body: "Seemed tired in the afternoon meeting",
			actor: "system",
			importance: 0.8,
		});
		const past = new Date(Date.now() - 14 * 86_400_000);
		await sql`
			UPDATE node SET last_accessed_at = ${past}, access_count = 0 WHERE id = ${obs.id}
		`;
		await applyForgettingCurves({ sql, bus });

		const rows = await sql`SELECT importance FROM node WHERE id = ${obs.id}`;
		const after = rows[0]?.["importance"] as number;
		// Observations have a 14-day half-life -> approx 0.4.
		expect(after).toBeCloseTo(0.4, 1);
	});

	test("event node halves after ~14 days", async () => {
		const ev = await nodes.create({
			kind: "event",
			body: "Dentist appointment at 3pm",
			actor: "user",
			importance: 0.8,
		});
		const past = new Date(Date.now() - 14 * 86_400_000);
		await sql`
			UPDATE node SET last_accessed_at = ${past}, access_count = 0 WHERE id = ${ev.id}
		`;
		await applyForgettingCurves({ sql, bus });

		const rows = await sql`SELECT importance FROM node WHERE id = ${ev.id}`;
		const after = rows[0]?.["importance"] as number;
		expect(after).toBeCloseTo(0.4, 1);
	});

	test("preference and observation with same age have different decay", async () => {
		const [pref, obs] = await Promise.all([
			nodes.create({
				kind: "preference",
				body: "Prefers quiet over loud meetings always",
				actor: "user",
				importance: 0.8,
			}),
			nodes.create({
				kind: "observation",
				body: "Seemed distracted during the standup today",
				actor: "user",
				importance: 0.8,
			}),
		]);
		const past = new Date(Date.now() - 30 * 86_400_000);
		await sql`UPDATE node SET last_accessed_at = ${past} WHERE id = ${pref.id}`;
		await sql`UPDATE node SET last_accessed_at = ${past} WHERE id = ${obs.id}`;
		await applyForgettingCurves({ sql, bus });

		const rows = await sql`
			SELECT id, importance FROM node WHERE id IN (${pref.id}, ${obs.id})
		`;
		const prefAfter = rows.find((r) => (r["id"] as number) === Number(pref.id))?.[
			"importance"
		] as number;
		const obsAfter = rows.find((r) => (r["id"] as number) === Number(obs.id))?.[
			"importance"
		] as number;
		expect(prefAfter).toBeGreaterThan(obsAfter);
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
