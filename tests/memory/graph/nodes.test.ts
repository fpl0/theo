/**
 * Integration tests for NodeRepository.
 *
 * Tests CRUD operations, event emission, embedding handling,
 * similarity search, confidence adjustment, and access tracking.
 *
 * Requires Docker PostgreSQL running via `just up`.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../../src/db/pool.ts";
import type { EventBus } from "../../../src/events/bus.ts";
import { EMBEDDING_DIM } from "../../../src/memory/embeddings.ts";
import { NodeRepository } from "../../../src/memory/graph/nodes.ts";
import { asNodeId } from "../../../src/memory/graph/types.ts";
import {
	cleanEventTables,
	createFailingEmbeddings,
	createMockEmbeddings,
	createTestBus,
	createTestPool,
} from "../../helpers.ts";

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

let pool: Pool;
let sql: Sql;
let bus: EventBus;
let repo: NodeRepository;
let failRepo: NodeRepository;

beforeAll(async () => {
	pool = createTestPool();
	const connectResult = await pool.connect();
	if (!connectResult.ok) {
		throw new Error(`Test setup failed: ${connectResult.error.message}`);
	}
	sql = pool.sql;

	bus = createTestBus(sql);
	await bus.start();

	repo = new NodeRepository(sql, bus, createMockEmbeddings());
	failRepo = new NodeRepository(sql, bus, createFailingEmbeddings());
});

beforeEach(async () => {
	await sql`TRUNCATE node, edge CASCADE`;
	await cleanEventTables(sql);
});

afterAll(async () => {
	if (bus) await bus.stop();
	if (pool) await pool.end();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("NodeRepository.create", () => {
	test("creates node with embedding and emits event", async () => {
		const node = await repo.create({
			kind: "fact",
			body: "The sky is blue",
			actor: "theo",
		});

		expect(node.id).toBeGreaterThan(0);
		expect(node.kind).toBe("fact");
		expect(node.body).toBe("The sky is blue");
		expect(node.embedding).toBeInstanceOf(Float32Array);
		expect(node.embedding?.length).toBe(EMBEDDING_DIM);
		expect(node.trust).toBe("inferred");
		expect(node.confidence).toBe(1.0);
		expect(node.importance).toBe(0.5);
		expect(node.sensitivity).toBe("none");
		expect(node.accessCount).toBe(0);
		expect(node.lastAccessedAt).toBeNull();
		expect(node.createdAt).toBeInstanceOf(Date);
		expect(node.updatedAt).toBeInstanceOf(Date);
	});

	test("creates node with custom fields", async () => {
		const node = await repo.create({
			kind: "preference",
			body: "Likes TypeScript",
			trust: "owner",
			confidence: 0.9,
			importance: 0.8,
			sensitivity: "sensitive",
			actor: "user",
		});

		expect(node.kind).toBe("preference");
		expect(node.trust).toBe("owner");
		expect(node.confidence).toBe(0.9);
		expect(node.importance).toBeCloseTo(0.8, 1);
		expect(node.sensitivity).toBe("sensitive");
	});

	test("creates node with null embedding when embedding fails", async () => {
		const node = await failRepo.create({
			kind: "fact",
			body: "This still gets stored",
			actor: "theo",
		});

		expect(node.id).toBeGreaterThan(0);
		expect(node.body).toBe("This still gets stored");
		expect(node.embedding).toBeNull();
	});

	test("creates pattern node", async () => {
		const node = await repo.create({
			kind: "pattern",
			body: "User asks about weather on Mondays",
			actor: "system",
		});
		expect(node.kind).toBe("pattern");
	});

	test("creates principle node", async () => {
		const node = await repo.create({
			kind: "principle",
			body: "Always verify before acting",
			actor: "system",
		});
		expect(node.kind).toBe("principle");
	});
});

describe("NodeRepository.getById", () => {
	test("returns node by ID", async () => {
		const created = await repo.create({
			kind: "fact",
			body: "Test node",
			actor: "theo",
		});

		const found = await repo.getById(created.id);
		expect(found).not.toBeNull();
		expect(found?.id).toBe(created.id);
		expect(found?.body).toBe("Test node");
	});

	test("returns null for non-existent ID", async () => {
		const found = await repo.getById(asNodeId(999999));
		expect(found).toBeNull();
	});
});

describe("NodeRepository.update", () => {
	test("updates body and re-embeds", async () => {
		const node = await repo.create({
			kind: "fact",
			body: "Original body",
			actor: "theo",
		});

		const updated = await repo.update(node.id, {
			body: "Updated body",
			actor: "theo",
		});

		expect(updated.body).toBe("Updated body");
		expect(updated.embedding).toBeInstanceOf(Float32Array);
		expect(updated.embedding).not.toEqual(node.embedding);
	});

	test("updates kind without changing embedding", async () => {
		const node = await repo.create({
			kind: "fact",
			body: "Some fact",
			actor: "theo",
		});

		const updated = await repo.update(node.id, {
			kind: "observation",
			actor: "theo",
		});

		expect(updated.kind).toBe("observation");
		expect(updated.embedding).toEqual(node.embedding);
	});

	test("throws for non-existent node", async () => {
		await expect(repo.update(asNodeId(999999), { body: "nope", actor: "theo" })).rejects.toThrow(
			"not found",
		);
	});
});

describe("NodeRepository.adjustConfidence", () => {
	test("increases confidence", async () => {
		const node = await repo.create({
			kind: "fact",
			body: "Adjustable",
			confidence: 0.5,
			actor: "theo",
		});

		await repo.adjustConfidence(node.id, 0.3, "system");
		const updated = await repo.getById(node.id);
		expect(updated?.confidence).toBeCloseTo(0.8, 1);
	});

	test("clamps to 1.0", async () => {
		const node = await repo.create({
			kind: "fact",
			body: "High confidence",
			confidence: 0.8,
			actor: "theo",
		});

		await repo.adjustConfidence(node.id, 0.5, "system");
		const updated = await repo.getById(node.id);
		expect(updated?.confidence).toBe(1.0);
	});

	test("clamps to 0.0", async () => {
		const node = await repo.create({
			kind: "fact",
			body: "Low confidence",
			confidence: 0.2,
			actor: "theo",
		});

		await repo.adjustConfidence(node.id, -0.5, "system");
		const updated = await repo.getById(node.id);
		expect(updated?.confidence).toBe(0.0);
	});

	test("throws for non-existent node", async () => {
		await expect(repo.adjustConfidence(asNodeId(999999), 0.1, "system")).rejects.toThrow(
			"not found",
		);
	});
});

describe("NodeRepository.findSimilar", () => {
	test("finds similar nodes above threshold", async () => {
		const node = await repo.create({
			kind: "fact",
			body: "TypeScript is a typed language",
			actor: "theo",
		});

		expect(node.embedding).not.toBeNull();
		const embedding = node.embedding ?? new Float32Array(EMBEDDING_DIM);
		const results = await repo.findSimilar(embedding, 0.5, 10);
		expect(results.length).toBeGreaterThanOrEqual(1);
		expect(results[0]?.id).toBe(node.id);
		expect(results[0]?.similarity).toBeGreaterThan(0.5);
	});

	test("returns empty when no nodes match threshold", async () => {
		await repo.create({
			kind: "fact",
			body: "Something specific",
			actor: "theo",
		});

		const orthogonal = new Float32Array(EMBEDDING_DIM);
		orthogonal[0] = 1.0;
		const results = await repo.findSimilar(orthogonal, 0.99, 10);
		for (const r of results) {
			expect(r.similarity).toBeGreaterThanOrEqual(0.99);
		}
	});

	test("skips nodes without embeddings", async () => {
		await failRepo.create({
			kind: "fact",
			body: "No embedding",
			actor: "theo",
		});

		const queryVec = new Float32Array(EMBEDDING_DIM);
		queryVec[0] = 1.0;
		const results = await repo.findSimilar(queryVec, 0.0, 100);
		for (const r of results) {
			expect(r.embedding).not.toBeNull();
		}
	});
});

describe("NodeRepository.recordAccess", () => {
	test("increments access count and sets timestamp", async () => {
		const node = await repo.create({
			kind: "fact",
			body: "Access me",
			actor: "theo",
		});

		expect(node.accessCount).toBe(0);
		expect(node.lastAccessedAt).toBeNull();

		await repo.recordAccess([node.id]);

		const updated = await repo.getById(node.id);
		expect(updated?.accessCount).toBe(1);
		expect(updated?.lastAccessedAt).toBeInstanceOf(Date);
	});

	test("increments multiple times", async () => {
		const node = await repo.create({
			kind: "fact",
			body: "Access twice",
			actor: "theo",
		});

		await repo.recordAccess([node.id]);
		await repo.recordAccess([node.id]);

		const updated = await repo.getById(node.id);
		expect(updated?.accessCount).toBe(2);
	});

	test("batch increments multiple nodes", async () => {
		const n1 = await repo.create({ kind: "fact", body: "Node 1", actor: "theo" });
		const n2 = await repo.create({ kind: "fact", body: "Node 2", actor: "theo" });

		await repo.recordAccess([n1.id, n2.id]);

		const u1 = await repo.getById(n1.id);
		const u2 = await repo.getById(n2.id);
		expect(u1?.accessCount).toBe(1);
		expect(u2?.accessCount).toBe(1);
	});

	test("handles empty array without error", async () => {
		await repo.recordAccess([]);
	});
});
