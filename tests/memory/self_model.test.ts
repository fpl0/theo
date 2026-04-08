/**
 * Integration tests for SelfModelRepository.
 *
 * Tests prediction recording, outcome tracking, calibration computation,
 * and event emission. Runs against real PostgreSQL via `just up`.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import {
	createSelfModelRepository,
	type SelfModelRepository,
} from "../../src/memory/self_model.ts";
import { cleanEventTables, createTestBus, createTestPool } from "../helpers.ts";

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

let pool: Pool;
let sql: Sql;
let bus: EventBus;
let repo: SelfModelRepository;

beforeAll(async () => {
	pool = createTestPool();
	sql = pool.sql;
	bus = createTestBus(sql);
	await bus.start();
	repo = createSelfModelRepository(sql, bus);
});

beforeEach(async () => {
	await sql`TRUNCATE self_model_domain CASCADE`;
	await cleanEventTables(sql);
});

afterAll(async () => {
	if (bus) await bus.stop();
	if (pool) await pool.end();
});

// ---------------------------------------------------------------------------
// recordPrediction
// ---------------------------------------------------------------------------

describe("recordPrediction", () => {
	test("creates new domain with predictions=1", async () => {
		await repo.recordPrediction("scheduling", "theo");

		const domain = await repo.getDomain("scheduling");
		expect(domain).not.toBeNull();
		expect(domain?.predictions).toBe(1);
		expect(domain?.correct).toBe(0);
	});

	test("increments predictions for existing domain", async () => {
		await repo.recordPrediction("scheduling", "theo");
		await repo.recordPrediction("scheduling", "theo");
		await repo.recordPrediction("scheduling", "theo");

		const domain = await repo.getDomain("scheduling");
		expect(domain?.predictions).toBe(3);
		expect(domain?.correct).toBe(0);
	});

	test("emits memory.self_model.updated event", async () => {
		await repo.recordPrediction("scheduling", "theo");

		const events = await sql<{ type: string; data: Record<string, unknown> }[]>`
			SELECT type, data FROM events
			WHERE type = 'memory.self_model.updated'
			ORDER BY id DESC LIMIT 1
		`;
		const event = events[0];
		expect(event).toBeDefined();
		expect(event?.type).toBe("memory.self_model.updated");
		expect(event?.data).toMatchObject({ domain: "scheduling" });
	});
});

// ---------------------------------------------------------------------------
// recordOutcome
// ---------------------------------------------------------------------------

describe("recordOutcome", () => {
	test("correct outcome: increments correct counter", async () => {
		await repo.recordPrediction("drafting", "theo");
		await repo.recordOutcome("drafting", true, "theo");

		const domain = await repo.getDomain("drafting");
		expect(domain?.predictions).toBe(1);
		expect(domain?.correct).toBe(1);
	});

	test("incorrect outcome: correct counter unchanged", async () => {
		await repo.recordPrediction("drafting", "theo");
		await repo.recordOutcome("drafting", false, "theo");

		const domain = await repo.getDomain("drafting");
		expect(domain?.predictions).toBe(1);
		expect(domain?.correct).toBe(0);
	});

	test("emits event on correct outcome", async () => {
		await repo.recordPrediction("drafting", "theo");
		await cleanEventTables(sql);
		await repo.recordOutcome("drafting", true, "theo");

		const events = await sql<{ data: Record<string, unknown> }[]>`
			SELECT data FROM events
			WHERE type = 'memory.self_model.updated'
			ORDER BY id DESC LIMIT 1
		`;
		expect(events[0]?.data).toMatchObject({ domain: "drafting", correct: true });
	});

	test("throws on missing domain", async () => {
		await expect(repo.recordOutcome("nonexistent", true, "theo")).rejects.toThrow(
			"Self model domain 'nonexistent' not found",
		);
	});

	test("emits event on incorrect outcome", async () => {
		await repo.recordPrediction("drafting", "theo");
		await cleanEventTables(sql);
		await repo.recordOutcome("drafting", false, "theo");

		const events = await sql<{ data: Record<string, unknown> }[]>`
			SELECT data FROM events
			WHERE type = 'memory.self_model.updated'
			ORDER BY id DESC LIMIT 1
		`;
		expect(events[0]?.data).toMatchObject({ domain: "drafting", correct: false });
	});
});

// ---------------------------------------------------------------------------
// getCalibration
// ---------------------------------------------------------------------------

describe("getCalibration", () => {
	test("returns 0 for missing domain", async () => {
		const cal = await repo.getCalibration("nonexistent");
		expect(cal).toBe(0);
	});

	test("calibration 100%: 5 predictions, 5 correct", async () => {
		for (let i = 0; i < 5; i++) {
			await repo.recordPrediction("recommendations", "theo");
			await repo.recordOutcome("recommendations", true, "theo");
		}

		const cal = await repo.getCalibration("recommendations");
		expect(cal).toBeCloseTo(1.0, 4);
	});

	test("calibration 50%: 4 predictions, 2 correct", async () => {
		for (let i = 0; i < 4; i++) {
			await repo.recordPrediction("memory_relevance", "theo");
		}
		await repo.recordOutcome("memory_relevance", true, "theo");
		await repo.recordOutcome("memory_relevance", true, "theo");
		await repo.recordOutcome("memory_relevance", false, "theo");
		await repo.recordOutcome("memory_relevance", false, "theo");

		const cal = await repo.getCalibration("memory_relevance");
		expect(cal).toBeCloseTo(0.5, 4);
	});

	test("calibration 0%: predictions but no correct outcomes", async () => {
		for (let i = 0; i < 3; i++) {
			await repo.recordPrediction("mood_assessment", "theo");
			await repo.recordOutcome("mood_assessment", false, "theo");
		}

		const cal = await repo.getCalibration("mood_assessment");
		expect(cal).toBe(0);
	});
});

// ---------------------------------------------------------------------------
// getDomain
// ---------------------------------------------------------------------------

describe("getDomain", () => {
	test("returns null for missing domain", async () => {
		const domain = await repo.getDomain("nonexistent");
		expect(domain).toBeNull();
	});

	test("returns domain with correct fields", async () => {
		await repo.recordPrediction("scheduling", "theo");

		const domain = await repo.getDomain("scheduling");
		expect(domain).not.toBeNull();
		expect(domain?.name).toBe("scheduling");
		expect(domain?.predictions).toBe(1);
		expect(domain?.correct).toBe(0);
		expect(domain?.createdAt).toBeInstanceOf(Date);
		expect(domain?.updatedAt).toBeInstanceOf(Date);
	});
});
