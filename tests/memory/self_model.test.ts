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
	isWindowDue,
	type SelfModelRepository,
	WINDOW_DAYS,
	WINDOW_MAX_PREDICTIONS,
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
		expect(domain?.recentPredictions).toBe(1);
		expect(domain?.recentCorrect).toBe(0);
		expect(domain?.windowResetAt).toBeInstanceOf(Date);
		expect(domain?.createdAt).toBeInstanceOf(Date);
		expect(domain?.updatedAt).toBeInstanceOf(Date);
	});
});

// ---------------------------------------------------------------------------
// Windowed calibration (Phase 13a)
// ---------------------------------------------------------------------------

describe("windowed calibration (Phase 13a)", () => {
	test("recent_* counters track alongside lifetime counters", async () => {
		await repo.recordPrediction("drafting", "theo");
		await repo.recordOutcome("drafting", true, "theo");
		await repo.recordPrediction("drafting", "theo");
		await repo.recordOutcome("drafting", false, "theo");

		const domain = await repo.getDomain("drafting");
		expect(domain?.predictions).toBe(2);
		expect(domain?.correct).toBe(1);
		expect(domain?.recentPredictions).toBe(2);
		expect(domain?.recentCorrect).toBe(1);
	});

	test("getCalibration returns the windowed ratio", async () => {
		for (let i = 0; i < 4; i++) {
			await repo.recordPrediction("recommendations", "theo");
		}
		await repo.recordOutcome("recommendations", true, "theo");
		await repo.recordOutcome("recommendations", true, "theo");
		await repo.recordOutcome("recommendations", true, "theo");
		await repo.recordOutcome("recommendations", false, "theo");

		const windowed = await repo.getCalibration("recommendations");
		expect(windowed).toBeCloseTo(0.75, 4);
	});

	test("getLifetimeCalibration returns the cumulative ratio", async () => {
		await repo.recordPrediction("scheduling", "theo");
		await repo.recordOutcome("scheduling", true, "theo");
		await repo.recordPrediction("scheduling", "theo");
		await repo.recordOutcome("scheduling", false, "theo");

		const lifetime = await repo.getLifetimeCalibration("scheduling");
		expect(lifetime).toBeCloseTo(0.5, 4);
	});

	test("50-prediction burst resets the window on the 51st", async () => {
		for (let i = 0; i < WINDOW_MAX_PREDICTIONS; i++) {
			await repo.recordPrediction("drafting", "theo");
			await repo.recordOutcome("drafting", true, "theo");
		}

		const before = await repo.getDomain("drafting");
		expect(before?.recentPredictions).toBe(WINDOW_MAX_PREDICTIONS);
		expect(before?.recentCorrect).toBe(WINDOW_MAX_PREDICTIONS);

		// The 51st prediction should roll the window over and start fresh.
		await repo.recordPrediction("drafting", "theo");
		const after = await repo.getDomain("drafting");
		expect(after?.recentPredictions).toBe(1);
		expect(after?.recentCorrect).toBe(0);
		// Lifetime counters keep going up.
		expect(after?.predictions).toBe(WINDOW_MAX_PREDICTIONS + 1);
		expect(after?.correct).toBe(WINDOW_MAX_PREDICTIONS);
	});

	test("stale window timestamp triggers reset on next prediction", async () => {
		await repo.recordPrediction("drafting", "theo");
		await repo.recordOutcome("drafting", true, "theo");

		// Backdate the window to simulate 31+ days of inactivity.
		await sql`
			UPDATE self_model_domain
			SET window_reset_at = now() - (${WINDOW_DAYS + 1}::bigint * interval '1 day')
			WHERE name = 'drafting'
		`;

		await repo.recordPrediction("drafting", "theo");

		const after = await repo.getDomain("drafting");
		expect(after?.recentPredictions).toBe(1);
		expect(after?.recentCorrect).toBe(0);
		// Lifetime counters remain intact.
		expect(after?.predictions).toBe(2);
		expect(after?.correct).toBe(1);
	});

	test("isWindowDue: detects prediction-count trigger", () => {
		expect(isWindowDue(new Date(), WINDOW_MAX_PREDICTIONS)).toBe(true);
		expect(isWindowDue(new Date(), WINDOW_MAX_PREDICTIONS - 1)).toBe(false);
	});

	test("isWindowDue: detects staleness trigger", () => {
		const now = new Date("2026-04-21T00:00:00Z");
		const stale = new Date(now.getTime() - (WINDOW_DAYS + 1) * 86_400 * 1000);
		const fresh = new Date(now.getTime() - 10 * 86_400 * 1000);
		expect(isWindowDue(stale, 1, now)).toBe(true);
		expect(isWindowDue(fresh, 1, now)).toBe(false);
	});
});
