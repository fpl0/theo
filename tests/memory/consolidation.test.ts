/**
 * Integration tests for consolidation (episode compression + node merging).
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import type { AbstractionDeps } from "../../src/memory/abstraction.ts";
import {
	type ConsolidationDeps,
	compressOldEpisodes,
	consolidate,
	mergeNodes,
	registerEpisodeSummarizedApplier,
} from "../../src/memory/consolidation.ts";
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

function makeAbstractionDeps(): AbstractionDeps {
	return { sql, bus, nodes, edges, synthesizer: async () => "NONE" };
}

function makeConsolidationDeps(overrides?: Partial<ConsolidationDeps>): ConsolidationDeps {
	return {
		sql,
		bus,
		episodic,
		abstraction: makeAbstractionDeps(),
		forgetting: { sql, bus },
		propagation: { sql, bus },
		summarizer: async () => "short summary",
		...overrides,
	};
}

describe("compressOldEpisodes", () => {
	test("compresses episodes older than cutoff", async () => {
		const sessionId = "session-old";
		for (let i = 0; i < 3; i++) {
			const ep = await episodic.append({
				sessionId,
				role: "user",
				body: `msg ${String(i)}`,
				actor: "user",
			});
			await sql`UPDATE episode SET created_at = now() - interval '10 days' WHERE id = ${ep.id}`;
		}

		// Register the applier so episode.summarized events get applied.
		const localBus = createTestBus(sql);
		registerEpisodeSummarizedApplier({ bus: localBus, sql, episodic });
		await localBus.start();

		const compressed = await compressOldEpisodes(
			makeConsolidationDeps({ bus: localBus }),
			async () => "consolidated summary",
		);
		expect(compressed).toBeGreaterThan(0);
		await localBus.flush();

		const summaryRows = await sql`
			SELECT body FROM episode WHERE body = 'consolidated summary' AND superseded_by IS NULL
		`;
		expect(summaryRows).toHaveLength(1);

		const supersededRows = await sql`
			SELECT 1 FROM episode WHERE superseded_by IS NOT NULL AND session_id = ${sessionId}
		`;
		expect(supersededRows.length).toBe(3);
		await localBus.stop();
	});

	test("recent episodes are left alone", async () => {
		await episodic.append({
			sessionId: "session-recent",
			role: "user",
			body: "new",
			actor: "user",
		});

		const compressed = await compressOldEpisodes(
			makeConsolidationDeps(),
			async () => "never called",
		);
		expect(compressed).toBe(0);
	});
});

describe("mergeNodes", () => {
	test("edges are redirected and losing node is soft-deleted", async () => {
		const keeper = await nodes.create({
			kind: "fact",
			body: "survivor",
			actor: "user",
			confidence: 0.9,
		});
		const retired = await nodes.create({
			kind: "fact",
			body: "loser",
			actor: "user",
			confidence: 0.5,
		});
		const anchor = await nodes.create({ kind: "fact", body: "anchor", actor: "user" });
		await edges.create({
			sourceId: retired.id,
			targetId: anchor.id,
			label: "related_to",
			weight: 1.0,
			actor: "user",
		});

		const result = await mergeNodes(keeper.id, retired.id, { sql, bus });
		expect(result.keptId).toBe(keeper.id);
		expect(result.retiredId).toBe(retired.id);

		const redirected = await sql`
			SELECT 1 FROM edge WHERE source_id = ${keeper.id} AND target_id = ${anchor.id}
			  AND valid_to IS NULL
		`;
		expect(redirected.length).toBe(1);

		const loserConf = await sql`SELECT confidence FROM node WHERE id = ${retired.id}`;
		expect(loserConf[0]?.["confidence"]).toBe(0);

		const mergedInto = await sql`
			SELECT 1 FROM edge WHERE source_id = ${retired.id} AND target_id = ${keeper.id}
			  AND label = 'merged_into'
		`;
		expect(mergedInto.length).toBe(1);

		const eventRows = await sql`SELECT 1 FROM events WHERE type = 'memory.node.merged'`;
		expect(eventRows.length).toBe(1);
	});

	test("episode_node references are redirected", async () => {
		const keeper = await nodes.create({
			kind: "fact",
			body: "keeper",
			actor: "user",
			confidence: 0.9,
		});
		const retired = await nodes.create({
			kind: "fact",
			body: "retired",
			actor: "user",
			confidence: 0.3,
		});
		const episode = await episodic.append({
			sessionId: "session-merge",
			role: "user",
			body: "mentioned",
			actor: "user",
		});
		await episodic.linkToNode(episode.id, retired.id);

		await mergeNodes(keeper.id, retired.id, { sql, bus });

		const rows = await sql`
			SELECT node_id FROM episode_node WHERE episode_id = ${episode.id}
		`;
		const keeperMatches = rows.filter((row) => (row["node_id"] as number) === Number(keeper.id));
		expect(keeperMatches.length).toBe(1);
		const retiredMatches = rows.filter((row) => (row["node_id"] as number) === Number(retired.id));
		expect(retiredMatches.length).toBe(0);
	});
});

describe("consolidate end-to-end", () => {
	test("returns a result summary even when nothing to do", async () => {
		const result = await consolidate(makeConsolidationDeps());
		expect(result.episodesCompressed).toBe(0);
		expect(result.nodesMerged).toBe(0);
		expect(result.errors).toHaveLength(0);
	});

	test("errors in one stage surface in the errors array", async () => {
		// Simulate a stage failure by pointing forgetting at a closed/unreachable
		// SQL handle. The orchestrator catches and reports the error without
		// aborting sibling stages.
		const brokenSql = {
			begin: async () => {
				throw new Error("simulated sql failure");
			},
		} as unknown as Sql;

		const deps = makeConsolidationDeps({
			forgetting: { sql: brokenSql, bus },
		});
		const result = await consolidate(deps);
		expect(result.errors.length).toBeGreaterThan(0);
		expect(result.errors[0]).toContain("simulated sql failure");
	});
});
