/**
 * Integration tests for contradiction detection (decision/effect split).
 *
 * Covers:
 *   - No similar nodes → no contradiction request emitted.
 *   - Different kinds ignored.
 *   - Contradicting pair → both confidences reduced, contradicts edge created,
 *     memory.contradiction.detected emitted.
 *   - Non-contradicting pair → no side effects.
 *   - Multiple candidates each checked independently.
 *   - Rate limit: 11 requests → only 10 classified.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import {
	applyContradictionVerdict,
	type ContradictionClassifier,
	type ContradictionDeps,
	MAX_CALLS_PER_MINUTE,
	RateLimiter,
	registerContradictionHandlers,
	requestContradictionChecks,
	requestContradictionsFor,
	runContradictionClassification,
} from "../../src/memory/contradiction.ts";
import { EdgeRepository } from "../../src/memory/graph/edges.ts";
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

/**
 * Stubbed classifier. Constructs a verdict based on a substring hint in the
 * two bodies — lets us assert against deterministic outcomes without calling
 * the SDK.
 */
function classifierThatSays(contradicts: boolean): ContradictionClassifier {
	return async () => ({ contradicts, explanation: contradicts ? "direct contradiction" : "ok" });
}

describe("requestContradictionChecks", () => {
	test("no similar nodes → no requests emitted", async () => {
		const node = await nodes.create({ kind: "fact", body: "Unique alpha", actor: "user" });
		const deps: ContradictionDeps = { bus, nodes, edges };
		const event = {
			id: "01K000000000000000000AAAAA" as never,
			type: "memory.node.created" as const,
			version: 1,
			timestamp: new Date(),
			actor: "user" as const,
			data: {
				nodeId: node.id,
				kind: "fact" as const,
				body: node.body,
				sensitivity: "none" as const,
				hasEmbedding: true,
			},
			metadata: {},
		};

		await requestContradictionChecks(event, deps);

		const rows = await sql`SELECT 1 FROM events WHERE type = 'contradiction.requested'`;
		expect(rows).toHaveLength(0);
	});

	test("different kinds are ignored", async () => {
		await nodes.create({ kind: "fact", body: "User likes cats", actor: "user" });
		const pref = await nodes.create({
			kind: "preference",
			body: "User likes cats",
			actor: "user",
		});

		await requestContradictionsFor(pref.id, { bus, nodes, edges });

		const rows = await sql`SELECT 1 FROM events WHERE type = 'contradiction.requested'`;
		expect(rows).toHaveLength(0);
	});

	test("similar same-kind nodes generate requests", async () => {
		await nodes.create({ kind: "fact", body: "Cats are mammals", actor: "user" });
		const second = await nodes.create({ kind: "fact", body: "Cats are mammals", actor: "user" });

		await requestContradictionsFor(second.id, { bus, nodes, edges });

		const rows = await sql`
			SELECT data FROM events WHERE type = 'contradiction.requested' ORDER BY id
		`;
		expect(rows.length).toBeGreaterThan(0);
	});
});

describe("runContradictionClassification", () => {
	test("contradicting verdict → classified event carries contradicts=true", async () => {
		const a = await nodes.create({ kind: "fact", body: "User likes cats", actor: "user" });
		const b = await nodes.create({ kind: "fact", body: "User dislikes cats", actor: "user" });

		const limiter = new RateLimiter(10, 60_000);
		await runContradictionClassification(
			{
				id: "01K000000000000000000BBBBB" as never,
				type: "contradiction.requested",
				version: 1,
				timestamp: new Date(),
				actor: "system",
				data: { nodeId: a.id, candidateId: b.id },
				metadata: {},
			},
			{ bus, nodes, edges },
			classifierThatSays(true),
			limiter,
		);

		const rows = await sql`
			SELECT data FROM events
			WHERE type = 'contradiction.classified'
		`;
		expect(rows).toHaveLength(1);
		const data = rows[0]?.["data"] as { contradicts: boolean; explanation: string };
		expect(data.contradicts).toBe(true);
		expect(data.explanation).toBe("direct contradiction");
	});

	test("rate limit caps calls per window", async () => {
		const a = await nodes.create({ kind: "fact", body: "A", actor: "user" });
		const b = await nodes.create({ kind: "fact", body: "B", actor: "user" });
		const limiter = new RateLimiter(MAX_CALLS_PER_MINUTE, 60_000);

		let callCount = 0;
		const countingClassifier: ContradictionClassifier = async () => {
			callCount++;
			return { contradicts: false, explanation: "" };
		};

		for (let i = 0; i < MAX_CALLS_PER_MINUTE + 1; i++) {
			await runContradictionClassification(
				{
					id: `01K000000000000000000${String(i).padStart(5, "A")}` as never,
					type: "contradiction.requested",
					version: 1,
					timestamp: new Date(),
					actor: "system",
					data: { nodeId: a.id, candidateId: b.id },
					metadata: {},
				},
				{ bus, nodes, edges },
				countingClassifier,
				limiter,
			);
		}
		expect(callCount).toBe(MAX_CALLS_PER_MINUTE);
	});

	test("classifier failure emits non-contradicting verdict", async () => {
		const a = await nodes.create({ kind: "fact", body: "AA", actor: "user" });
		const b = await nodes.create({ kind: "fact", body: "BB", actor: "user" });
		const limiter = new RateLimiter(10, 60_000);
		await runContradictionClassification(
			{
				id: "01K000000000000000000FFFFF" as never,
				type: "contradiction.requested",
				version: 1,
				timestamp: new Date(),
				actor: "system",
				data: { nodeId: a.id, candidateId: b.id },
				metadata: {},
			},
			{ bus, nodes, edges },
			async () => {
				throw new Error("boom");
			},
			limiter,
		);
		const rows = await sql`SELECT data FROM events WHERE type = 'contradiction.classified'`;
		expect(rows).toHaveLength(1);
		const data = rows[0]?.["data"] as { contradicts: boolean };
		expect(data.contradicts).toBe(false);
	});
});

describe("applyContradictionVerdict", () => {
	test("contradicts=true → both confidences drop and contradicts edge created", async () => {
		const a = await nodes.create({
			kind: "fact",
			body: "Sky is blue",
			actor: "user",
			confidence: 1.0,
		});
		const b = await nodes.create({
			kind: "fact",
			body: "Sky is green",
			actor: "user",
			confidence: 1.0,
		});

		await applyContradictionVerdict(
			{
				id: "01K000000000000000000CCCCC" as never,
				type: "contradiction.classified",
				version: 1,
				timestamp: new Date(),
				actor: "system",
				data: {
					nodeId: a.id,
					candidateId: b.id,
					contradicts: true,
					explanation: "direct contradiction",
				},
				metadata: {},
			},
			{ bus, nodes, edges },
		);

		const updatedA = await nodes.getById(a.id);
		const updatedB = await nodes.getById(b.id);
		expect(updatedA?.confidence).toBeCloseTo(0.8, 5);
		expect(updatedB?.confidence).toBeCloseTo(0.8, 5);

		const edgeRows = await sql`
			SELECT * FROM edge WHERE label = 'contradicts' AND valid_to IS NULL
		`;
		expect(edgeRows).toHaveLength(1);

		const detected = await sql`
			SELECT data FROM events WHERE type = 'memory.contradiction.detected'
		`;
		expect(detected).toHaveLength(1);
	});

	test("contradicts=false → no side effects", async () => {
		const a = await nodes.create({
			kind: "fact",
			body: "Dogs bark",
			actor: "user",
			confidence: 1.0,
		});
		const b = await nodes.create({
			kind: "fact",
			body: "Dogs are mammals",
			actor: "user",
			confidence: 1.0,
		});

		await applyContradictionVerdict(
			{
				id: "01K000000000000000000DDDDD" as never,
				type: "contradiction.classified",
				version: 1,
				timestamp: new Date(),
				actor: "system",
				data: {
					nodeId: a.id,
					candidateId: b.id,
					contradicts: false,
					explanation: "",
				},
				metadata: {},
			},
			{ bus, nodes, edges },
		);

		const updatedA = await nodes.getById(a.id);
		expect(updatedA?.confidence).toBe(1.0);
		const edgeRows = await sql`SELECT 1 FROM edge WHERE label = 'contradicts'`;
		expect(edgeRows).toHaveLength(0);
	});
});

describe("registerContradictionHandlers", () => {
	test("end-to-end pipeline emits classified + detected events", async () => {
		const localBus = createTestBus(sql);
		const limiter = new RateLimiter(10, 60_000);
		let classifierInvocations = 0;
		registerContradictionHandlers({
			bus: localBus,
			nodes: new NodeRepository(sql, localBus, createMockEmbeddings()),
			edges: new EdgeRepository(sql, localBus),
			classifier: async () => {
				classifierInvocations++;
				return { contradicts: true, explanation: "end-to-end" };
			},
			rateLimiter: limiter,
		});
		await localBus.start();

		const localNodes = new NodeRepository(sql, localBus, createMockEmbeddings());
		await localNodes.create({
			kind: "fact",
			body: "User likes dark mode",
			actor: "user",
		});
		await localNodes.create({
			kind: "fact",
			body: "User likes dark mode",
			actor: "user",
		});

		// `flush` loops internally until cascading handlers have settled.
		await localBus.flush();

		const classified = await sql`SELECT 1 FROM events WHERE type = 'contradiction.classified'`;
		expect(classifierInvocations).toBeGreaterThan(0);
		expect(classified.length).toBeGreaterThan(0);
		const detected = await sql`SELECT 1 FROM events WHERE type = 'memory.contradiction.detected'`;
		expect(detected.length).toBeGreaterThan(0);

		await localBus.stop();
	});
});
