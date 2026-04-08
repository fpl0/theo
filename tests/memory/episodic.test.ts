/**
 * Integration tests for EpisodicRepository.
 *
 * Tests episode append with embedding, session queries, superseded filtering,
 * and node linking. Uses mock embeddings — no real ONNX model loaded.
 *
 * Requires Docker PostgreSQL running via `just up`.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { EpisodicRepository } from "../../src/memory/episodic.ts";
import { NodeRepository } from "../../src/memory/graph/nodes.ts";
import {
	cleanEventTables,
	createMockEmbeddings,
	createTestBus,
	createTestPool,
} from "../helpers.ts";

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

let pool: Pool;
let sql: Sql;
let bus: EventBus;
let repo: EpisodicRepository;
let nodeRepo: NodeRepository;

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
	repo = new EpisodicRepository(sql, bus, embeddings);
	nodeRepo = new NodeRepository(sql, bus, embeddings);
});

beforeEach(async () => {
	await sql`TRUNCATE episode, episode_node, node, edge CASCADE`;
	await cleanEventTables(sql);
});

afterAll(async () => {
	if (bus) await bus.stop();
	if (pool) await pool.end();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("EpisodicRepository.append", () => {
	test("appends episode with embedding and emits event", async () => {
		const episode = await repo.append({
			sessionId: "sess-1",
			role: "user",
			body: "Hello, Theo!",
			actor: "user",
		});

		expect(episode.id).toBeGreaterThan(0);
		expect(episode.sessionId).toBe("sess-1");
		expect(episode.role).toBe("user");
		expect(episode.body).toBe("Hello, Theo!");
		expect(episode.supersededBy).toBeNull();
		expect(episode.createdAt).toBeInstanceOf(Date);
	});

	test("appends assistant episode", async () => {
		const episode = await repo.append({
			sessionId: "sess-1",
			role: "assistant",
			body: "Hello! How can I help?",
			actor: "theo",
		});

		expect(episode.role).toBe("assistant");
		expect(episode.body).toBe("Hello! How can I help?");
	});

	test("atomicity: episode and event are committed together", async () => {
		const episode = await repo.append({
			sessionId: "sess-atomic",
			role: "user",
			body: "Atomicity test",
			actor: "user",
		});

		// Episode persisted
		const rows = await sql`SELECT * FROM episode WHERE id = ${episode.id}`;
		expect(rows.length).toBe(1);

		// Event persisted
		const events = await sql`
			SELECT * FROM events WHERE type = 'memory.episode.created'
			ORDER BY id DESC LIMIT 1
		`;
		expect(events.length).toBe(1);
		const data = (events[0] as Record<string, unknown>)["data"] as Record<string, unknown>;
		expect(data["episodeId"]).toBe(episode.id);
	});
});

describe("EpisodicRepository.getBySession", () => {
	test("returns episodes in chronological order", async () => {
		await repo.append({ sessionId: "sess-order", role: "user", body: "First", actor: "user" });
		await repo.append({
			sessionId: "sess-order",
			role: "assistant",
			body: "Second",
			actor: "theo",
		});
		await repo.append({ sessionId: "sess-order", role: "user", body: "Third", actor: "user" });

		const episodes = await repo.getBySession("sess-order");
		expect(episodes.length).toBe(3);
		expect(episodes[0]?.body).toBe("First");
		expect(episodes[1]?.body).toBe("Second");
		expect(episodes[2]?.body).toBe("Third");
	});

	test("excludes superseded episodes", async () => {
		const e1 = await repo.append({
			sessionId: "sess-super",
			role: "user",
			body: "Original",
			actor: "user",
		});
		const e2 = await repo.append({
			sessionId: "sess-super",
			role: "assistant",
			body: "Summary",
			actor: "theo",
		});

		// Mark e1 as superseded by e2 (simulates Phase 13 consolidation)
		await sql`UPDATE episode SET superseded_by = ${e2.id} WHERE id = ${e1.id}`;

		const episodes = await repo.getBySession("sess-super");
		expect(episodes.length).toBe(1);
		expect(episodes[0]?.id).toBe(e2.id);
	});

	test("returns only episodes from specified session", async () => {
		await repo.append({ sessionId: "sess-a", role: "user", body: "Session A", actor: "user" });
		await repo.append({ sessionId: "sess-b", role: "user", body: "Session B", actor: "user" });

		const episodesA = await repo.getBySession("sess-a");
		expect(episodesA.length).toBe(1);
		expect(episodesA[0]?.body).toBe("Session A");
	});

	test("returns empty for non-existent session", async () => {
		const episodes = await repo.getBySession("does-not-exist");
		expect(episodes.length).toBe(0);
	});
});

describe("EpisodicRepository.linkToNode", () => {
	test("links episode to node", async () => {
		const episode = await repo.append({
			sessionId: "sess-link",
			role: "user",
			body: "Link me to a node",
			actor: "user",
		});
		const node = await nodeRepo.create({
			kind: "fact",
			body: "Linked fact",
			actor: "theo",
		});

		await repo.linkToNode(episode.id, node.id);

		const rows = await sql`
			SELECT * FROM episode_node
			WHERE episode_id = ${episode.id} AND node_id = ${node.id}
		`;
		expect(rows.length).toBe(1);
	});

	test("linkToNode is idempotent", async () => {
		const episode = await repo.append({
			sessionId: "sess-idem",
			role: "user",
			body: "Idempotent link",
			actor: "user",
		});
		const node = await nodeRepo.create({
			kind: "fact",
			body: "Idempotent node",
			actor: "theo",
		});

		await repo.linkToNode(episode.id, node.id);
		await repo.linkToNode(episode.id, node.id); // Second call — no error

		const rows = await sql`
			SELECT * FROM episode_node
			WHERE episode_id = ${episode.id} AND node_id = ${node.id}
		`;
		expect(rows.length).toBe(1);
	});
});
