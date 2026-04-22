/**
 * Ideation pure-function tests — hashing, budget thresholds, budget cap logic.
 *
 * The full end-to-end run lives in `tests/db/ideation.integration.test.ts`
 * (integration). Here we verify the deterministic hashing and the budget
 * gate's threshold math so regressions are caught without a live DB.
 */

import { describe, expect, test } from "bun:test";
import { DEFAULT_BUDGET, hashProposal } from "../../src/ideation/run.ts";
import { IDEATION_MAX_LEVEL as STORE_CAP } from "../../src/proposals/store.ts";

describe("hashProposal", () => {
	test("normalization: whitespace + case does not change the hash", async () => {
		const a = await hashProposal("Hello World");
		const b = await hashProposal("  hello  world  ");
		const c = await hashProposal("HELLO WORLD");
		expect(a).toBe(b);
		expect(a).toBe(c);
	});

	test("different content produces different hashes", async () => {
		const a = await hashProposal("buy a telescope");
		const b = await hashProposal("buy a microscope");
		expect(a).not.toBe(b);
	});

	test("hash is deterministic 64-char hex", async () => {
		const h = await hashProposal("test");
		expect(h).toMatch(/^[0-9a-f]{64}$/);
	});
});

describe("DEFAULT_BUDGET", () => {
	test("per-week cap matches plan (3)", () => {
		expect(DEFAULT_BUDGET.maxRunsPerWeek).toBe(3);
	});
	test("per-run cap matches plan ($0.50)", () => {
		expect(DEFAULT_BUDGET.maxBudgetUsdPerRun).toBe(0.5);
	});
	test("per-month cap matches plan ($10.00)", () => {
		expect(DEFAULT_BUDGET.maxBudgetUsdPerMonth).toBe(10);
	});
	test("dedup window matches plan (30 days)", () => {
		expect(DEFAULT_BUDGET.dedupWindowDays).toBe(30);
	});
	test("rejection backoff doubles (multiplier = 2.0)", () => {
		expect(DEFAULT_BUDGET.rejectionBackoffMultiplier).toBe(2);
	});
});

describe("ideation autonomy cap", () => {
	test("ideation cap is level 2 per §11", () => {
		expect(STORE_CAP).toBe(2);
	});
});
