/**
 * SkillRepository integration tests.
 *
 * Exercises create (with and without parent), findByTrigger ordering and
 * promoted-exclusion, recordOutcome counter bumps, and promote() idempotency.
 * Emits are asserted against the real event log.
 *
 * Runs against PostgreSQL via `just test-db`.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { Sql } from "postgres";
import type { Pool } from "../../src/db/pool.ts";
import type { EventBus } from "../../src/events/bus.ts";
import { createSkillRepository, type SkillRepository } from "../../src/memory/skills.ts";
import {
	cleanEventTables,
	createMockEmbeddings,
	createTestBus,
	createTestPool,
} from "../helpers.ts";

let pool: Pool;
let sql: Sql;
let bus: EventBus;
let repo: SkillRepository;

beforeAll(async () => {
	pool = createTestPool();
	sql = pool.sql;
	bus = createTestBus(sql);
	await bus.start();
	repo = createSkillRepository(sql, createMockEmbeddings(), bus);
});

beforeEach(async () => {
	await sql`TRUNCATE skill CASCADE`;
	await cleanEventTables(sql);
});

afterAll(async () => {
	if (bus) await bus.stop();
	if (pool) await pool.end();
});

// Count events of a given type in the log — checkpoints are independent so
// truncating the events table between tests is the source of truth.
async function countEvents(type: string): Promise<number> {
	const rows = await sql<{ count: string }[]>`
		SELECT COUNT(*)::text AS count FROM events WHERE type = ${type}
	`;
	return Number(rows[0]?.count ?? 0);
}

// ---------------------------------------------------------------------------
// create
// ---------------------------------------------------------------------------

describe("SkillRepository.create", () => {
	test("creates a skill with version=1 when no parent given", async () => {
		const skill = await repo.create({
			name: "pair-programming",
			trigger: "when asked to debug",
			strategy: "narrate the hypothesis before changing code",
		});
		expect(skill.id).toBeGreaterThan(0);
		expect(skill.version).toBe(1);
		expect(skill.parentId).toBeNull();
		expect(skill.promotedAt).toBeNull();
		expect(skill.successCount).toBe(0);
		expect(skill.attemptCount).toBe(0);
	});

	test("emits memory.skill.created", async () => {
		await repo.create({
			name: "plan-first",
			trigger: "before any large change",
			strategy: "write a short plan before editing",
		});
		expect(await countEvents("memory.skill.created")).toBe(1);
	});

	test("refinement inherits parent version + 1 and records parent_id", async () => {
		const v1 = await repo.create({
			name: "plan-first",
			trigger: "before any large change",
			strategy: "write a one-line plan",
		});
		const v2 = await repo.create({
			name: "plan-first",
			trigger: "before any large change",
			strategy: "write a numbered checklist",
			parentId: v1.id,
		});
		expect(v2.version).toBe(2);
		expect(v2.parentId).toBe(v1.id);
	});

	test("refinement against a missing parent throws", async () => {
		await expect(
			repo.create({
				name: "orphan",
				trigger: "trigger",
				strategy: "strategy",
				parentId: 999_999,
			}),
		).rejects.toThrow(/parent skill #999999 not found/);
	});
});

// ---------------------------------------------------------------------------
// findByTrigger
// ---------------------------------------------------------------------------

describe("SkillRepository.findByTrigger", () => {
	test("returns matching skills ordered by similarity", async () => {
		await repo.create({
			name: "a",
			trigger: "apples are sweet",
			strategy: "eat them",
		});
		await repo.create({
			name: "b",
			trigger: "bananas are yellow",
			strategy: "peel them",
		});

		const results = await repo.findByTrigger("apples are sweet", 5);
		expect(results.length).toBe(2);
		// The closer match comes first (identical trigger for "a").
		expect(results[0]?.name).toBe("a");
	});

	test("excludes promoted skills", async () => {
		const promoted = await repo.create({
			name: "promoted",
			trigger: "was promoted",
			strategy: "legacy",
		});
		await repo.promote(promoted.id);

		await repo.create({
			name: "active",
			trigger: "was promoted",
			strategy: "current",
		});

		const results = await repo.findByTrigger("was promoted", 10);
		const names = results.map((r) => r.name);
		expect(names).toContain("active");
		expect(names).not.toContain("promoted");
	});

	test("respects the limit argument", async () => {
		for (let i = 0; i < 5; i++) {
			await repo.create({
				name: `skill-${String(i)}`,
				trigger: `trigger ${String(i)}`,
				strategy: `strategy ${String(i)}`,
			});
		}
		const results = await repo.findByTrigger("trigger", 2);
		expect(results.length).toBe(2);
	});
});

// ---------------------------------------------------------------------------
// recordOutcome
// ---------------------------------------------------------------------------

describe("SkillRepository.recordOutcome", () => {
	test("success increments both success_count and attempt_count", async () => {
		const skill = await repo.create({
			name: "s",
			trigger: "t",
			strategy: "s",
		});
		const after = await repo.recordOutcome(skill.id, true);
		expect(after.successCount).toBe(1);
		expect(after.attemptCount).toBe(1);
		expect(after.successRate).toBeCloseTo(1);
	});

	test("failure increments attempt_count only", async () => {
		const skill = await repo.create({
			name: "s",
			trigger: "t",
			strategy: "s",
		});
		const after = await repo.recordOutcome(skill.id, false);
		expect(after.successCount).toBe(0);
		expect(after.attemptCount).toBe(1);
		expect(after.successRate).toBeCloseTo(0);
	});

	test("multiple outcomes accumulate the success rate", async () => {
		const skill = await repo.create({
			name: "s",
			trigger: "t",
			strategy: "s",
		});
		await repo.recordOutcome(skill.id, true);
		await repo.recordOutcome(skill.id, true);
		await repo.recordOutcome(skill.id, false);
		const final = await repo.recordOutcome(skill.id, true);
		expect(final.successCount).toBe(3);
		expect(final.attemptCount).toBe(4);
		expect(final.successRate).toBeCloseTo(0.75);
	});

	test("recordOutcome does not emit an event", async () => {
		const skill = await repo.create({
			name: "s",
			trigger: "t",
			strategy: "s",
		});
		await cleanEventTables(sql);
		await repo.recordOutcome(skill.id, true);
		expect(await countEvents("memory.skill.created")).toBe(0);
		expect(await countEvents("memory.skill.promoted")).toBe(0);
	});

	test("throws when the skill does not exist", async () => {
		await expect(repo.recordOutcome(999_999, true)).rejects.toThrow(/not found/);
	});
});

// ---------------------------------------------------------------------------
// promote
// ---------------------------------------------------------------------------

describe("SkillRepository.promote", () => {
	test("sets promoted_at and emits memory.skill.promoted", async () => {
		const skill = await repo.create({
			name: "star",
			trigger: "top performer",
			strategy: "always works",
		});
		await cleanEventTables(sql);
		const promoted = await repo.promote(skill.id);
		expect(promoted.promotedAt).not.toBeNull();
		expect(await countEvents("memory.skill.promoted")).toBe(1);
	});

	test("double-promote is idempotent — promoted_at preserved", async () => {
		const skill = await repo.create({
			name: "star",
			trigger: "top performer",
			strategy: "always works",
		});
		const first = await repo.promote(skill.id);
		const again = await repo.promote(skill.id);
		expect(again.promotedAt?.getTime()).toBe(first.promotedAt?.getTime());
	});

	test("throws when the skill does not exist", async () => {
		await expect(repo.promote(999_999)).rejects.toThrow(/not found/);
	});
});

// ---------------------------------------------------------------------------
// getById
// ---------------------------------------------------------------------------

describe("SkillRepository.getById", () => {
	test("returns the skill when it exists", async () => {
		const skill = await repo.create({
			name: "findable",
			trigger: "can locate",
			strategy: "by id",
		});
		const found = await repo.getById(skill.id);
		expect(found?.id).toBe(skill.id);
		expect(found?.name).toBe("findable");
	});

	test("returns null for missing ids", async () => {
		expect(await repo.getById(999_999)).toBeNull();
	});
});
