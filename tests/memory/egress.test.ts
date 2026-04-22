/**
 * Egress privacy filter tests — `src/memory/egress.ts`.
 *
 * The pure `filterOutgoingPrompt` function is the workhorse; DB-backed
 * consent grant/revoke paths are exercised against the test database in
 * `tests/db/`. Here we verify the pure decision table for every turn
 * class and sensitivity.
 */

import { describe, expect, test } from "bun:test";
import { type AssembledPromptForEgress, filterOutgoingPrompt } from "../../src/memory/egress.ts";

const dims: AssembledPromptForEgress = {
	userModelDimensions: [
		{ name: "communication_style", egressSensitivity: "public" },
		{ name: "values", egressSensitivity: "private" },
		{ name: "shadow_patterns", egressSensitivity: "local_only" },
	],
};

describe("filterOutgoingPrompt", () => {
	test("interactive turn with consent: public + private included, local_only stripped", () => {
		const d = filterOutgoingPrompt(dims, "interactive", {
			autonomousCloudEgressEnabled: true,
		});
		expect(d.allowed).toBe(true);
		expect(d.includedDimensions).toContain("communication_style");
		expect(d.includedDimensions).toContain("values");
		expect(d.strippedDimensions).toContain("shadow_patterns");
		expect(d.strippedDimensions).not.toContain("values");
	});

	test("interactive turn without consent still proceeds (local egress)", () => {
		const d = filterOutgoingPrompt(dims, "interactive", {
			autonomousCloudEgressEnabled: false,
		});
		expect(d.allowed).toBe(true);
	});

	test("ideation turn without consent: blocked with no_consent", () => {
		const d = filterOutgoingPrompt(dims, "ideation", {
			autonomousCloudEgressEnabled: false,
		});
		expect(d.allowed).toBe(false);
		if (!d.allowed) {
			expect(d.reason).toBe("no_consent");
		}
	});

	test("ideation turn with consent: public only; private + local_only stripped", () => {
		const d = filterOutgoingPrompt(dims, "ideation", {
			autonomousCloudEgressEnabled: true,
		});
		expect(d.allowed).toBe(true);
		expect(d.includedDimensions).toEqual(["communication_style"]);
		expect(d.strippedDimensions).toContain("values");
		expect(d.strippedDimensions).toContain("shadow_patterns");
	});

	test("reflex turn with consent: same as ideation", () => {
		const d = filterOutgoingPrompt(dims, "reflex", {
			autonomousCloudEgressEnabled: true,
		});
		expect(d.allowed).toBe(true);
		expect(d.strippedDimensions).toContain("values");
		expect(d.strippedDimensions).toContain("shadow_patterns");
	});

	test("executive turn without consent: blocked", () => {
		const d = filterOutgoingPrompt(dims, "executive", {
			autonomousCloudEgressEnabled: false,
		});
		expect(d.allowed).toBe(false);
	});

	test("local_only always stripped, regardless of consent", () => {
		const d = filterOutgoingPrompt(dims, "interactive", {
			autonomousCloudEgressEnabled: true,
		});
		expect(d.includedDimensions).not.toContain("shadow_patterns");
	});

	test("empty dimensions list: allowed, empty lists", () => {
		const d = filterOutgoingPrompt({ userModelDimensions: [] }, "interactive", {
			autonomousCloudEgressEnabled: true,
		});
		expect(d.allowed).toBe(true);
		expect(d.includedDimensions).toEqual([]);
		expect(d.strippedDimensions).toEqual([]);
	});
});
