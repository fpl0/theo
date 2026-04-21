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
	groupByTopic,
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

describe("compressOldEpisodes importance gate (Phase 13a)", () => {
	test("high-importance episodes are preserved at full fidelity", async () => {
		const sessionId = "session-preserve";
		const preserved = await episodic.append({
			sessionId,
			role: "user",
			body: "this is critical",
			actor: "user",
			importance: 0.9,
		});
		await sql`
			UPDATE episode SET created_at = now() - interval '10 days'
			WHERE id = ${preserved.id}
		`;

		const localBus = createTestBus(sql);
		registerEpisodeSummarizedApplier({ bus: localBus, sql, episodic });
		await localBus.start();

		const compressed = await compressOldEpisodes(
			makeConsolidationDeps({ bus: localBus }),
			async () => "should not be called",
		);
		expect(compressed).toBe(0);
		await localBus.flush();

		const stillActive = await sql`
			SELECT id FROM episode WHERE id = ${preserved.id} AND superseded_by IS NULL
		`;
		expect(stillActive).toHaveLength(1);
		await localBus.stop();
	});

	test("low-importance episodes are still compressed", async () => {
		const sessionId = "session-compress";
		const compressible = await episodic.append({
			sessionId,
			role: "user",
			body: "ordinary turn",
			actor: "user",
			importance: 0.3,
		});
		await sql`
			UPDATE episode SET created_at = now() - interval '10 days'
			WHERE id = ${compressible.id}
		`;

		const localBus = createTestBus(sql);
		registerEpisodeSummarizedApplier({ bus: localBus, sql, episodic });
		await localBus.start();

		const compressed = await compressOldEpisodes(
			makeConsolidationDeps({ bus: localBus }),
			async () => "low-importance summary",
		);
		expect(compressed).toBeGreaterThan(0);
		await localBus.flush();

		const superseded = await sql`
			SELECT 1 FROM episode WHERE id = ${compressible.id} AND superseded_by IS NOT NULL
		`;
		expect(superseded).toHaveLength(1);
		await localBus.stop();
	});
});

describe("groupByTopic (Phase 13a)", () => {
	const baseDate = new Date("2026-01-01T00:00:00Z");
	const makeEp = (id: number, sessionId: string) => ({
		id,
		sessionId,
		role: "user" as const,
		body: `body-${String(id)}`,
		createdAt: baseDate,
	});

	test("two topics in one session produce two groups", () => {
		const episodes = [makeEp(1, "s"), makeEp(2, "s"), makeEp(3, "s"), makeEp(4, "s")];
		// 1 and 2 share node 100; 3 and 4 share node 200.
		const links = new Map<number, readonly number[]>([
			[1, [100]],
			[2, [100]],
			[3, [200]],
			[4, [200]],
		]);
		const groups = groupByTopic(episodes, links);
		expect(groups.size).toBe(2);
		const sizes = [...groups.values()].map((g) => g.length).sort();
		expect(sizes).toEqual([2, 2]);
	});

	test("transitive sharing unions into a single group", () => {
		const episodes = [makeEp(1, "s"), makeEp(2, "s"), makeEp(3, "s")];
		// 1 <- node A -> 2; 2 <- node B -> 3. Union-find should produce one group.
		const links = new Map<number, readonly number[]>([
			[1, [10]],
			[2, [10, 20]],
			[3, [20]],
		]);
		const groups = groupByTopic(episodes, links);
		expect(groups.size).toBe(1);
	});

	test("episodes with no node links fall back to per-session grouping", () => {
		const episodes = [makeEp(1, "s1"), makeEp(2, "s1"), makeEp(3, "s2")];
		const links = new Map<number, readonly number[]>();
		const groups = groupByTopic(episodes, links);
		expect(groups.size).toBe(2);
		const s1 = groups.get("session:s1");
		const s2 = groups.get("session:s2");
		expect(s1?.length).toBe(2);
		expect(s2?.length).toBe(1);
	});

	test("mix of topic-linked and unlinked episodes produces both kinds of groups", () => {
		const episodes = [makeEp(1, "s"), makeEp(2, "s"), makeEp(3, "s")];
		// 1 and 2 share a node; 3 has no links.
		const links = new Map<number, readonly number[]>([
			[1, [42]],
			[2, [42]],
		]);
		const groups = groupByTopic(episodes, links);
		expect(groups.size).toBe(2);
		// One group is a "topic:*" key; the other is "session:s".
		const keys = [...groups.keys()].sort();
		expect(keys.some((k) => k.startsWith("topic:"))).toBe(true);
		expect(keys.some((k) => k.startsWith("session:"))).toBe(true);
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
