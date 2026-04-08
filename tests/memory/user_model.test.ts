/**
 * Integration tests for UserModelRepository.
 *
 * Tests dimension CRUD, confidence computation from evidence thresholds,
 * and event emission. Runs against real PostgreSQL via `just up`.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import {
	createUserModelRepository,
	getThreshold,
	type UserModelRepository,
} from "../../src/memory/user_model.ts";
import { cleanEventTables, createTestBus, createTestPool } from "../helpers.ts";

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

let pool: Pool;
let sql: Sql;
let bus: EventBus;
let repo: UserModelRepository;

beforeAll(async () => {
	pool = createTestPool();
	sql = pool.sql;
	bus = createTestBus(sql);
	await bus.start();
	repo = createUserModelRepository(sql, bus);
});

beforeEach(async () => {
	await sql`TRUNCATE user_model_dimension CASCADE`;
	await cleanEventTables(sql);
});

afterAll(async () => {
	if (bus) await bus.stop();
	if (pool) await pool.end();
});

// ---------------------------------------------------------------------------
// getThreshold
// ---------------------------------------------------------------------------

describe("getThreshold", () => {
	test("returns known threshold for communication_style", () => {
		expect(getThreshold("communication_style")).toBe(5);
	});

	test("returns known threshold for individuation_markers", () => {
		expect(getThreshold("individuation_markers")).toBe(30);
	});

	test("returns known threshold for boundaries", () => {
		expect(getThreshold("boundaries")).toBe(3);
	});

	test("returns _default threshold for unknown dimension", () => {
		expect(getThreshold("custom_dim")).toBe(10);
	});
});

// ---------------------------------------------------------------------------
// updateDimension & getDimension
// ---------------------------------------------------------------------------

describe("updateDimension", () => {
	test("create new dimension: evidence=1, confidence=1/threshold", async () => {
		const dim = await repo.updateDimension("communication_style", "direct and concise", 1, "theo");

		expect(dim.name).toBe("communication_style");
		expect(dim.value).toBe("direct and concise");
		expect(dim.evidenceCount).toBe(1);
		expect(dim.confidence).toBeCloseTo(1 / 5, 4);
		expect(dim.threshold).toBe(5);
	});

	test("update existing dimension: increments evidence and recomputes confidence", async () => {
		await repo.updateDimension("communication_style", "direct", 1, "theo");
		const dim = await repo.updateDimension("communication_style", "direct and concise", 2, "theo");

		expect(dim.evidenceCount).toBe(3);
		expect(dim.confidence).toBeCloseTo(3 / 5, 4);
	});

	test("confidence caps at 1.0", async () => {
		const dim = await repo.updateDimension("boundaries", "no politics", 10, "theo");

		expect(dim.confidence).toBe(1.0);
		expect(dim.evidenceCount).toBe(10);
		expect(dim.threshold).toBe(3);
	});

	test("high-threshold dimension: low initial confidence", async () => {
		const dim = await repo.updateDimension(
			"individuation_markers",
			"early growth signals",
			1,
			"theo",
		);

		expect(dim.confidence).toBeCloseTo(1 / 30, 4);
		expect(dim.threshold).toBe(30);
	});

	test("unknown dimension uses _default threshold", async () => {
		const dim = await repo.updateDimension("custom_dim", "custom value", 5, "theo");

		expect(dim.confidence).toBeCloseTo(5 / 10, 4);
		expect(dim.threshold).toBe(10);
	});

	test("multiple evidence in one call", async () => {
		const dim = await repo.updateDimension("energy_patterns", "morning person", 3, "theo");

		expect(dim.evidenceCount).toBe(3);
		expect(dim.confidence).toBeCloseTo(3 / 10, 4);
	});

	test("JSONB value: object stored correctly", async () => {
		const value = { primary: "INTJ", big5: { openness: 0.8 } };
		const dim = await repo.updateDimension("personality_type", value, 1, "theo");

		expect(dim.value).toEqual(value);
	});

	test("emits memory.user_model.updated event", async () => {
		await repo.updateDimension("communication_style", "direct", 1, "theo");

		const events = await sql<{ type: string; data: Record<string, unknown> }[]>`
			SELECT type, data FROM events
			WHERE type = 'memory.user_model.updated'
			ORDER BY id DESC LIMIT 1
		`;
		const event = events[0];
		expect(event).toBeDefined();
		expect(event?.type).toBe("memory.user_model.updated");
		expect(event?.data).toMatchObject({ dimension: "communication_style" });
	});
});

// ---------------------------------------------------------------------------
// getDimension
// ---------------------------------------------------------------------------

describe("getDimension", () => {
	test("returns null for missing dimension", async () => {
		const dim = await repo.getDimension("nonexistent");
		expect(dim).toBeNull();
	});

	test("returns dimension by name", async () => {
		await repo.updateDimension("communication_style", "direct", 1, "theo");
		const dim = await repo.getDimension("communication_style");

		expect(dim).not.toBeNull();
		expect(dim?.name).toBe("communication_style");
		expect(dim?.value).toBe("direct");
	});
});

// ---------------------------------------------------------------------------
// getDimensions
// ---------------------------------------------------------------------------

describe("getDimensions", () => {
	test("returns empty array when no dimensions", async () => {
		const dims = await repo.getDimensions();
		expect(dims).toEqual([]);
	});

	test("returns all dimensions ordered by name", async () => {
		await repo.updateDimension("energy_patterns", "morning", 1, "theo");
		await repo.updateDimension("communication_style", "direct", 2, "theo");
		await repo.updateDimension("boundaries", "no politics", 1, "theo");

		const dims = await repo.getDimensions();
		expect(dims).toHaveLength(3);
		expect(dims.map((d) => d.name)).toEqual([
			"boundaries",
			"communication_style",
			"energy_patterns",
		]);

		// Verify confidence is computed per dimension
		const boundaries = dims.find((d) => d.name === "boundaries");
		expect(boundaries).toBeDefined();
		expect(boundaries?.confidence).toBeCloseTo(1 / 3, 4);
		expect(boundaries?.threshold).toBe(3);

		const comm = dims.find((d) => d.name === "communication_style");
		expect(comm).toBeDefined();
		expect(comm?.confidence).toBeCloseTo(2 / 5, 4);
		expect(comm?.threshold).toBe(5);
	});
});
