import { describe, expect, test } from "bun:test";
import { CONSOLIDATION_GATE } from "../../src/memory/salience.ts";

describe("CONSOLIDATION_GATE", () => {
	test("is between the neutral baseline and the 1.0 cap", () => {
		expect(CONSOLIDATION_GATE).toBeGreaterThan(0.5);
		expect(CONSOLIDATION_GATE).toBeLessThan(1.0);
	});
});
