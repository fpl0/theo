/**
 * Degradation ladder pure-function tests — `src/degradation/state.ts`.
 *
 * The ladder has five levels with strict allowance semantics. The pure
 * policy helpers are tested exhaustively; the singleton row path lives in
 * `tests/db/` against a real database.
 */

import { describe, expect, test } from "bun:test";
import {
	advisorAllowed,
	executiveAllowed,
	ideationAllowed,
	reflexAllowed,
} from "../../src/degradation/state.ts";

describe("ideationAllowed", () => {
	test("allowed at L0", () => {
		expect(ideationAllowed(0)).toBe(true);
	});
	test("allowed at L1", () => {
		expect(ideationAllowed(1)).toBe(true);
	});
	test("BLOCKED at L2 (ideation ceiling)", () => {
		expect(ideationAllowed(2)).toBe(false);
	});
	test("BLOCKED at L3", () => {
		expect(ideationAllowed(3)).toBe(false);
	});
	test("BLOCKED at L4", () => {
		expect(ideationAllowed(4)).toBe(false);
	});
});

describe("advisorAllowed", () => {
	test("interactive class: advisor available at L0..L3", () => {
		for (const level of [0, 1, 2, 3] as const) {
			expect(advisorAllowed(level, "interactive")).toBe(true);
		}
	});
	test("interactive class: advisor dropped at L4", () => {
		expect(advisorAllowed(4, "interactive")).toBe(false);
	});
	test("ideation class: advisor ONLY at L0 (drops at L1 per plan)", () => {
		expect(advisorAllowed(0, "ideation")).toBe(true);
		expect(advisorAllowed(1, "ideation")).toBe(false);
		expect(advisorAllowed(2, "ideation")).toBe(false);
	});
	test("reflex class: advisor at L0..L1; dropped at L2+", () => {
		expect(advisorAllowed(0, "reflex")).toBe(true);
		expect(advisorAllowed(1, "reflex")).toBe(true);
		expect(advisorAllowed(2, "reflex")).toBe(false);
		expect(advisorAllowed(3, "reflex")).toBe(false);
	});
	test("executive class: advisor at L0..L1; dropped at L2+", () => {
		expect(advisorAllowed(0, "executive")).toBe(true);
		expect(advisorAllowed(1, "executive")).toBe(true);
		expect(advisorAllowed(2, "executive")).toBe(false);
	});
});

describe("executiveAllowed", () => {
	test("allowed at L0..L2", () => {
		expect(executiveAllowed(0)).toBe(true);
		expect(executiveAllowed(1)).toBe(true);
		expect(executiveAllowed(2)).toBe(true);
	});
	test("BLOCKED at L3 (executive paused)", () => {
		expect(executiveAllowed(3)).toBe(false);
	});
	test("BLOCKED at L4", () => {
		expect(executiveAllowed(4)).toBe(false);
	});
});

describe("reflexAllowed", () => {
	test("allowed at L0..L3", () => {
		expect(reflexAllowed(0)).toBe(true);
		expect(reflexAllowed(1)).toBe(true);
		expect(reflexAllowed(2)).toBe(true);
		expect(reflexAllowed(3)).toBe(true);
	});
	test("BLOCKED at L4 (essential only)", () => {
		expect(reflexAllowed(4)).toBe(false);
	});
});
