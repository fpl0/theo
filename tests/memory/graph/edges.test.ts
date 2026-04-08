/**
 * Integration tests for EdgeRepository.
 *
 * Tests edge creation, expiration, temporal versioning (update = expire + create),
 * active filtering, and cascade behavior.
 *
 * Requires Docker PostgreSQL running via `just up`.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../../src/db/pool.ts";
import type { EventBus } from "../../../src/events/bus.ts";
import { EdgeRepository } from "../../../src/memory/graph/edges.ts";
import { NodeRepository } from "../../../src/memory/graph/nodes.ts";
import type { NodeId } from "../../../src/memory/graph/types.ts";
import { asEdgeId } from "../../../src/memory/graph/types.ts";
import {
	cleanEventTables,
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
let nodeRepo: NodeRepository;
let edgeRepo: EdgeRepository;

/** Helper: create two nodes and return their IDs. */
async function createNodePair(): Promise<{ sourceId: NodeId; targetId: NodeId }> {
	const source = await nodeRepo.create({ kind: "fact", body: "Source node", actor: "theo" });
	const target = await nodeRepo.create({ kind: "fact", body: "Target node", actor: "theo" });
	return { sourceId: source.id, targetId: target.id };
}

beforeAll(async () => {
	pool = createTestPool();
	const connectResult = await pool.connect();
	if (!connectResult.ok) {
		throw new Error(`Test setup failed: ${connectResult.error.message}`);
	}
	sql = pool.sql;

	bus = createTestBus(sql);
	await bus.start();

	nodeRepo = new NodeRepository(sql, bus, createMockEmbeddings());
	edgeRepo = new EdgeRepository(sql, bus);
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

describe("EdgeRepository.create", () => {
	test("creates active edge with defaults", async () => {
		const { sourceId, targetId } = await createNodePair();

		const edge = await edgeRepo.create({
			sourceId,
			targetId,
			label: "related_to",
			actor: "theo",
		});

		expect(edge.id).toBeGreaterThan(0);
		expect(edge.sourceId).toBe(sourceId);
		expect(edge.targetId).toBe(targetId);
		expect(edge.label).toBe("related_to");
		expect(edge.weight).toBe(1.0);
		expect(edge.validFrom).toBeInstanceOf(Date);
		expect(edge.validTo).toBeNull();
		expect(edge.createdAt).toBeInstanceOf(Date);
	});

	test("creates edge with custom weight", async () => {
		const { sourceId, targetId } = await createNodePair();

		const edge = await edgeRepo.create({
			sourceId,
			targetId,
			label: "caused_by",
			weight: 3.5,
			actor: "theo",
		});

		expect(edge.weight).toBe(3.5);
	});
});

describe("EdgeRepository.expire", () => {
	test("expires an active edge", async () => {
		const { sourceId, targetId } = await createNodePair();
		const edge = await edgeRepo.create({
			sourceId,
			targetId,
			label: "related_to",
			actor: "theo",
		});

		await edgeRepo.expire(edge.id, "theo");

		const fetched = await edgeRepo.getById(edge.id);
		expect(fetched?.validTo).toBeInstanceOf(Date);
	});

	test("throws when expiring non-existent edge", async () => {
		await expect(edgeRepo.expire(asEdgeId(999999), "theo")).rejects.toThrow("not found");
	});
});

describe("EdgeRepository.update (temporal versioning)", () => {
	test("expires old edge and creates new one", async () => {
		const { sourceId, targetId } = await createNodePair();
		const original = await edgeRepo.create({
			sourceId,
			targetId,
			label: "related_to",
			weight: 1.0,
			actor: "theo",
		});

		const updated = await edgeRepo.update(original.id, {
			weight: 2.5,
			actor: "theo",
		});

		expect(updated.id).not.toBe(original.id);
		expect(updated.weight).toBe(2.5);
		expect(updated.label).toBe("related_to");
		expect(updated.sourceId).toBe(sourceId);
		expect(updated.targetId).toBe(targetId);
		expect(updated.validTo).toBeNull();

		const old = await edgeRepo.getById(original.id);
		expect(old?.validTo).toBeInstanceOf(Date);
	});

	test("updates label while preserving endpoints", async () => {
		const { sourceId, targetId } = await createNodePair();
		const original = await edgeRepo.create({
			sourceId,
			targetId,
			label: "related_to",
			actor: "theo",
		});

		const updated = await edgeRepo.update(original.id, {
			label: "caused_by",
			actor: "theo",
		});

		expect(updated.label).toBe("caused_by");
		expect(updated.sourceId).toBe(sourceId);
		expect(updated.targetId).toBe(targetId);
	});

	test("throws for non-existent active edge", async () => {
		await expect(edgeRepo.update(asEdgeId(999999), { weight: 1.0, actor: "theo" })).rejects.toThrow(
			"not found",
		);
	});
});

describe("EdgeRepository.getActiveForNode", () => {
	test("returns only active edges", async () => {
		const { sourceId, targetId } = await createNodePair();

		const e1 = await edgeRepo.create({
			sourceId,
			targetId,
			label: "related_to",
			actor: "theo",
		});

		await edgeRepo.expire(e1.id, "theo");

		const third = await nodeRepo.create({ kind: "fact", body: "Third", actor: "theo" });
		await edgeRepo.create({
			sourceId,
			targetId: third.id,
			label: "leads_to",
			actor: "theo",
		});

		const active = await edgeRepo.getActiveForNode(sourceId);
		expect(active.length).toBe(1);
		expect(active[0]?.label).toBe("leads_to");
		for (const edge of active) {
			expect(edge.validTo).toBeNull();
		}
	});

	test("returns edges where node is source or target", async () => {
		const { sourceId, targetId } = await createNodePair();
		const third = await nodeRepo.create({ kind: "fact", body: "Third", actor: "theo" });

		await edgeRepo.create({ sourceId, targetId, label: "a", actor: "theo" });
		await edgeRepo.create({
			sourceId: third.id,
			targetId: sourceId,
			label: "b",
			actor: "theo",
		});

		const edges = await edgeRepo.getActiveForNode(sourceId);
		expect(edges.length).toBe(2);
	});
});

describe("edge cascade on node delete", () => {
	test("deleting a node cascades to its edges", async () => {
		const { sourceId, targetId } = await createNodePair();
		const edge = await edgeRepo.create({
			sourceId,
			targetId,
			label: "related_to",
			actor: "theo",
		});

		await sql`DELETE FROM node WHERE id = ${sourceId}`;

		const fetched = await edgeRepo.getById(edge.id);
		expect(fetched).toBeNull();
	});
});
