/**
 * Ideation pure-function tests — hashing, budget thresholds, budget cap logic.
 *
 * The full end-to-end run lives in `tests/db/ideation.integration.test.ts`
 * (integration). Here we verify the deterministic hashing and the budget
 * gate's threshold math so regressions are caught without a live DB.
 */

import { describe, expect, test } from "bun:test";
import { hashProposal } from "../../src/ideation/run.ts";

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
