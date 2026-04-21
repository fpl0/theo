/**
 * Integration tests for importance propagation.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { EdgeRepository } from "../../src/memory/graph/edges.ts";
import { NodeRepository } from "../../src/memory/graph/nodes.ts";
import {
	HOP_1_DELTA,
	HOP_2_DELTA,
	NORMALIZATION_THRESHOLD,
	normalizeImportance,
	propagateImportance,
} from "../../src/memory/propagation.ts";
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
	edges = new EdgeRepository(sql, bus);
});

beforeEach(async () => {
	await sql`TRUNCATE node, edge CASCADE`;
	await cleanEventTables(sql);
});

afterAll(async () => {
	await bus.stop();
	if (pool) await pool.end();
});

function makeAccessedEvent(nodeIds: readonly number[]): Parameters<typeof propagateImportance>[0] {
	return {
		id: "01K000000000000000000FFFFF" as never,
		type: "memory.node.accessed",
		version: 1,
		timestamp: new Date(),
		actor: "system",
		data: { nodeIds },
		metadata: {},
	};
}

describe("propagateImportance", () => {
	test("1-hop neighbour receives a boost", async () => {
		const a = await nodes.create({
			kind: "fact",
			body: "seed",
			actor: "user",
			importance: 0.5,
		});
		const b = await nodes.create({
			kind: "fact",
			body: "neighbor",
			actor: "user",
			importance: 0.3,
		});
		await edges.create({
			sourceId: a.id,
			targetId: b.id,
			label: "related_to",
			weight: 1.0,
			actor: "user",
		});

		await propagateImportance(makeAccessedEvent([a.id]), { sql, bus });

		const rows = await sql`SELECT importance FROM node WHERE id = ${b.id}`;
		const boosted = rows[0]?.["importance"] as number;
		expect(boosted).toBeCloseTo(0.3 + HOP_1_DELTA * 1.0, 3);
	});

	test("2-hop neighbour receives a smaller boost", async () => {
		const a = await nodes.create({
			kind: "fact",
			body: "A",
			actor: "user",
			importance: 0.5,
		});
		const b = await nodes.create({
			kind: "fact",
			body: "B",
			actor: "user",
			importance: 0.3,
		});
		const c = await nodes.create({
			kind: "fact",
			body: "C",
			actor: "user",
			importance: 0.3,
		});
		await edges.create({
			sourceId: a.id,
			targetId: b.id,
			label: "related_to",
			weight: 1.0,
			actor: "user",
		});
		await edges.create({
			sourceId: b.id,
			targetId: c.id,
			label: "related_to",
			weight: 1.0,
			actor: "user",
		});

		await propagateImportance(makeAccessedEvent([a.id]), { sql, bus });

		const cImportance = (await sql`SELECT importance FROM node WHERE id = ${c.id}`)[0]?.[
			"importance"
		] as number;
		// C is both a 2-hop neighbor of A (boost 0.01) and NOT a seed — so it
		// should get the HOP_2_DELTA only.
		expect(cImportance).toBeGreaterThan(0.3);
		expect(cImportance).toBeLessThanOrEqual(0.3 + HOP_1_DELTA + 0.0001);
	});

	test("does not boost seed node itself", async () => {
		const a = await nodes.create({
			kind: "fact",
			body: "seed",
			actor: "user",
			importance: 0.5,
		});
		const b = await nodes.create({
			kind: "fact",
			body: "neighbor",
			actor: "user",
			importance: 0.3,
		});
		await edges.create({
			sourceId: a.id,
			targetId: b.id,
			label: "related_to",
			weight: 1.0,
			actor: "user",
		});

		await propagateImportance(makeAccessedEvent([a.id]), { sql, bus });
		const rows = await sql`SELECT importance FROM node WHERE id = ${a.id}`;
		expect(rows[0]?.["importance"]).toBe(0.5);
	});

	test("boost capped at 1.0", async () => {
		const a = await nodes.create({
			kind: "fact",
			body: "seed",
			actor: "user",
			importance: 0.5,
		});
		const b = await nodes.create({
			kind: "fact",
			body: "near cap",
			actor: "user",
			importance: 0.999,
		});
		await edges.create({
			sourceId: a.id,
			targetId: b.id,
			label: "related_to",
			weight: 5.0, // maximum weight — boost would overshoot 1.0 without clamp.
			actor: "user",
		});

		await propagateImportance(makeAccessedEvent([a.id]), { sql, bus });
		const rows = await sql`SELECT importance FROM node WHERE id = ${b.id}`;
		expect(rows[0]?.["importance"]).toBeLessThanOrEqual(1.0);
	});
});

describe("normalizeImportance", () => {
	test("returns null when mean is below threshold", async () => {
		await nodes.create({ kind: "fact", body: "low", actor: "user", importance: 0.3 });
		const result = await normalizeImportance({ sql, bus });
		expect(result).toBeNull();
	});

	test("rescales when mean is above threshold", async () => {
		// Seed three high-importance nodes so mean > 0.6
		for (let i = 0; i < 3; i++) {
			await nodes.create({
				kind: "fact",
				body: `high-${String(i)}`,
				actor: "user",
				importance: 0.95,
			});
		}
		const mean = await normalizeImportance({ sql, bus });
		expect(mean).not.toBeNull();
		if (mean !== null) expect(mean).toBeGreaterThan(NORMALIZATION_THRESHOLD);

		const rows = await sql`SELECT AVG(importance)::real AS mean FROM node`;
		const newMean = rows[0]?.["mean"] as number;
		expect(newMean).toBeLessThan(NORMALIZATION_THRESHOLD);
	});
});

describe("HOP_2_DELTA constant", () => {
	// Exported to pin cognitive-science calibration — assert the invariant the
	// design document calls out (1-hop > 2-hop).
	test("HOP_1_DELTA > HOP_2_DELTA", () => {
		expect(HOP_1_DELTA).toBeGreaterThan(HOP_2_DELTA);
	});
});
