/**
 * Integration tests for the auto-edge discovery handler.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { newEventId } from "../../src/events/ids.ts";
import { discoverAutoEdges, MAX_CO_OCCURS_WEIGHT } from "../../src/memory/auto_edges.ts";
import { EpisodicRepository } from "../../src/memory/episodic.ts";
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
let episodic: EpisodicRepository;

beforeAll(async () => {
	pool = createTestPool();
	const connectResult = await pool.connect();
	if (!connectResult.ok) {
		throw new Error(`Test setup failed: ${connectResult.error.message}`);
	}
	sql = pool.sql;
	bus = createTestBus(sql);
	await bus.start();
	const embeddings = createMockEmbeddings();
	nodes = new NodeRepository(sql, bus, embeddings);
	edges = new EdgeRepository(sql, bus);
	episodic = new EpisodicRepository(sql, bus, embeddings);
});

beforeEach(async () => {
	await sql`TRUNCATE node, edge, episode, episode_node CASCADE`;
	await cleanEventTables(sql);
});

afterAll(async () => {
	await bus.stop();
	if (pool) await pool.end();
});

/** Wire two nodes into the same set of episodes in a single session. */
async function seedCoOccurrence(
	sessionId: string,
	episodeCount: number,
): Promise<{
	a: number;
	b: number;
}> {
	const a = await nodes.create({ kind: "fact", body: `a-${sessionId}`, actor: "user" });
	const b = await nodes.create({ kind: "fact", body: `b-${sessionId}`, actor: "user" });
	for (let i = 0; i < episodeCount; i++) {
		const episode = await episodic.append({
			sessionId,
			role: "user",
			body: `turn ${String(i)}`,
			actor: "user",
		});
		await episodic.linkToNode(episode.id, a.id);
		await episodic.linkToNode(episode.id, b.id);
	}
	return { a: a.id, b: b.id };
}

/** Synthesize a turn.completed event we can feed to the handler. */
function makeTurnCompleted(sessionId: string): Parameters<typeof discoverAutoEdges>[0] {
	return {
		id: newEventId(),
		type: "turn.completed",
		version: 1,
		timestamp: new Date(),
		actor: "theo",
		data: {
			sessionId,
			responseBody: "",
			durationMs: 0,
			inputTokens: 0,
			outputTokens: 0,
			totalTokens: 0,
			costUsd: 0,
		},
		metadata: {},
	};
}

describe("discoverAutoEdges", () => {
	test("creates co_occurs edge for new pair", async () => {
		await seedCoOccurrence("session-new", 1);
		await discoverAutoEdges(makeTurnCompleted("session-new"), { sql, bus, edges });
		const rows = await sql`
			SELECT weight FROM edge WHERE label = 'co_occurs' AND valid_to IS NULL
		`;
		expect(rows).toHaveLength(1);
		expect(rows[0]?.["weight"]).toBeCloseTo(0.5, 5);
	});

	test("strengthens existing co_occurs edge in subsequent ticks", async () => {
		const sessionId = "session-strengthen";
		await seedCoOccurrence(sessionId, 1);
		await discoverAutoEdges(makeTurnCompleted(sessionId), { sql, bus, edges });
		// Add another episode linking the same pair and re-run.
		const a = await sql`SELECT id FROM node WHERE body = ${`a-${sessionId}`}`;
		const b = await sql`SELECT id FROM node WHERE body = ${`b-${sessionId}`}`;
		const aId = a[0]?.["id"] as number;
		const bId = b[0]?.["id"] as number;
		const episode = await episodic.append({
			sessionId,
			role: "user",
			body: "another",
			actor: "user",
		});
		await episodic.linkToNode(episode.id, aId);
		await episodic.linkToNode(episode.id, bId);
		await discoverAutoEdges(makeTurnCompleted(sessionId), { sql, bus, edges });

		const rows = await sql`
			SELECT weight FROM edge WHERE label = 'co_occurs' AND valid_to IS NULL
		`;
		// Second run sees 2 co-occurrences → delta = 1.0. Plus initial 0.5 = 1.5.
		expect(rows).toHaveLength(1);
		expect((rows[0]?.["weight"] as number) > 0.5).toBe(true);
	});

	test("weight saturates at MAX_CO_OCCURS_WEIGHT", async () => {
		const sessionId = "session-saturate";
		await seedCoOccurrence(sessionId, 20);
		await discoverAutoEdges(makeTurnCompleted(sessionId), { sql, bus, edges });
		// Second run to push past the cap.
		await discoverAutoEdges(makeTurnCompleted(sessionId), { sql, bus, edges });
		const rows = await sql`
			SELECT weight FROM edge WHERE label = 'co_occurs' AND valid_to IS NULL
		`;
		expect(rows[0]?.["weight"]).toBeLessThanOrEqual(MAX_CO_OCCURS_WEIGHT);
		expect(rows[0]?.["weight"]).toBe(MAX_CO_OCCURS_WEIGHT);
	});

	test("does not create self-edges", async () => {
		const session = "session-self";
		const a = await nodes.create({ kind: "fact", body: "lone", actor: "user" });
		const ep = await episodic.append({
			sessionId: session,
			role: "user",
			body: "lone",
			actor: "user",
		});
		await episodic.linkToNode(ep.id, a.id);

		await discoverAutoEdges(makeTurnCompleted(session), { sql, bus, edges });
		const rows = await sql`SELECT 1 FROM edge WHERE label = 'co_occurs'`;
		expect(rows).toHaveLength(0);
	});

	test("edges from other sessions are not touched", async () => {
		await seedCoOccurrence("session-other", 3);
		await seedCoOccurrence("session-target", 1);

		await discoverAutoEdges(makeTurnCompleted("session-target"), { sql, bus, edges });
		const rows = await sql`
			SELECT source_id, target_id FROM edge WHERE label = 'co_occurs' AND valid_to IS NULL
		`;
		// Only the target session's pair shows up.
		expect(rows).toHaveLength(1);
	});
});
