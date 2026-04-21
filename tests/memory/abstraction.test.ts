/**
 * Integration tests for abstraction hierarchy synthesis.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import {
	NO_PATTERN_SENTINEL,
	type PatternSynthesizer,
	synthesizeAbstractions,
} from "../../src/memory/abstraction.ts";
import { EdgeRepository } from "../../src/memory/graph/edges.ts";
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

/** Three mutually-connected fact nodes with strong edges. */
async function seedTriangle(): Promise<readonly NodeId[]> {
	const a = await nodes.create({ kind: "fact", body: "fact A", actor: "user" });
	const b = await nodes.create({ kind: "fact", body: "fact B", actor: "user" });
	const c = await nodes.create({ kind: "fact", body: "fact C", actor: "user" });
	await edges.create({
		sourceId: a.id,
		targetId: b.id,
		label: "related_to",
		weight: 2.0,
		actor: "user",
	});
	await edges.create({
		sourceId: b.id,
		targetId: c.id,
		label: "related_to",
		weight: 2.0,
		actor: "user",
	});
	await edges.create({
		sourceId: a.id,
		targetId: c.id,
		label: "related_to",
		weight: 2.0,
		actor: "user",
	});
	return [a.id, b.id, c.id];
}

describe("synthesizeAbstractions", () => {
	test("creates a pattern node from a cluster of 3 related facts", async () => {
		await seedTriangle();
		const synthesizer: PatternSynthesizer = async () => "All three facts point to X.";

		const created = await synthesizeAbstractions({
			sql,
			bus,
			nodes,
			edges,
			synthesizer,
		});

		expect(created).toBeGreaterThan(0);

		const patterns = await sql`SELECT body FROM node WHERE kind = 'pattern'`;
		expect(patterns.length).toBeGreaterThan(0);

		const abstractionEdges = await sql`
			SELECT 1 FROM edge WHERE label = 'abstracted_from' AND valid_to IS NULL
		`;
		expect(abstractionEdges.length).toBeGreaterThan(0);
	});

	test("returns 0 when clusters do not exist", async () => {
		await nodes.create({ kind: "fact", body: "isolated", actor: "user" });
		const synthesizer: PatternSynthesizer = async () => "never called";

		const created = await synthesizeAbstractions({
			sql,
			bus,
			nodes,
			edges,
			synthesizer,
		});
		expect(created).toBe(0);
	});

	test("skips clusters that already have a pattern", async () => {
		const triangle = await seedTriangle();
		// Manually create a pattern and link it.
		const pattern = await nodes.create({
			kind: "pattern",
			body: "existing pattern",
			actor: "system",
		});
		for (const id of triangle) {
			await edges.create({
				sourceId: pattern.id,
				targetId: id,
				label: "abstracted_from",
				weight: 1.0,
				actor: "system",
			});
		}

		const synthesizer: PatternSynthesizer = async () => "would be new";
		const created = await synthesizeAbstractions({
			sql,
			bus,
			nodes,
			edges,
			synthesizer,
		});
		expect(created).toBe(0);
	});

	test("LLM returning NONE skips the cluster", async () => {
		await seedTriangle();
		const synthesizer: PatternSynthesizer = async () => NO_PATTERN_SENTINEL;

		const created = await synthesizeAbstractions({
			sql,
			bus,
			nodes,
			edges,
			synthesizer,
		});
		expect(created).toBe(0);
	});
});
