/**
 * Subagent catalog validation.
 *
 * The catalog is mostly static — these tests pin the shape so accidental
 * edits (missing prompt, silent model rename, tool leakage) fail loudly.
 */

import { describe, expect, test } from "bun:test";
import {
	buildSdkAgentsMap,
	SUBAGENTS,
	type SubagentName,
	toSdkAgentDefinition,
} from "../../src/chat/subagents.ts";

const MODEL_ALIASES = new Set(["opus", "sonnet", "haiku"]);
const ADVISOR_ELIGIBLE: ReadonlySet<SubagentName> = new Set<SubagentName>([
	"main",
	"planner",
	"coder",
	"researcher",
	"writer",
]);

describe("SUBAGENTS catalog", () => {
	test("Core 1 subagents are defined", () => {
		const names = Object.keys(SUBAGENTS).sort();
		expect(names).toEqual(
			["coder", "main", "planner", "psychologist", "researcher", "writer"].sort(),
		);
	});

	for (const [name, def] of Object.entries(SUBAGENTS)) {
		describe(name, () => {
			test("model is a valid alias", () => {
				expect(def.model).toBeDefined();
				expect(MODEL_ALIASES.has(def.model ?? "")).toBe(true);
			});

			test("maxTurns is positive", () => {
				expect(def.maxTurns).toBeDefined();
				expect(def.maxTurns ?? 0).toBeGreaterThan(0);
			});

			test("prompt is non-empty", () => {
				expect(typeof def.prompt).toBe("string");
				expect(def.prompt.length).toBeGreaterThan(0);
			});

			test("description is non-empty", () => {
				expect(typeof def.description).toBe("string");
				expect(def.description.length).toBeGreaterThan(0);
			});

			test("tools field is not specified (inherits parent tools)", () => {
				expect(def.tools).toBeUndefined();
			});
		});
	}

	test("plan-then-execute subagents carry advisorModel", () => {
		for (const name of ADVISOR_ELIGIBLE) {
			expect(SUBAGENTS[name].advisorModel).toBeDefined();
		}
	});

	test("psychologist already runs Opus — no separate advisor", () => {
		expect(SUBAGENTS.psychologist.advisorModel).toBeUndefined();
	});

	test("advisorModel is the Opus 4.6 identifier when set", () => {
		for (const name of ADVISOR_ELIGIBLE) {
			expect(SUBAGENTS[name].advisorModel).toBe("claude-opus-4-6");
		}
	});

	test("psychologist prompt invokes Jungian framework", () => {
		const prompt = SUBAGENTS.psychologist.prompt;
		expect(prompt.toLowerCase()).toContain("jungian");
		expect(prompt.toLowerCase()).toContain("shadow");
	});
});

describe("toSdkAgentDefinition", () => {
	test("strips advisorModel from the definition", () => {
		const sdk = toSdkAgentDefinition(SUBAGENTS.planner);
		expect(sdk).not.toHaveProperty("advisorModel");
		expect(sdk.model).toBe(SUBAGENTS.planner.model);
		expect(sdk.prompt).toBe(SUBAGENTS.planner.prompt);
		expect(sdk.description).toBe(SUBAGENTS.planner.description);
	});
});

describe("SDK agents map", () => {
	test("includes every subagent keyed by name", () => {
		const map = buildSdkAgentsMap();
		const keys = Object.keys(map).sort();
		expect(keys).toEqual(Object.keys(SUBAGENTS).sort());
	});

	test("every entry is an SDK-safe AgentDefinition (no advisorModel leak)", () => {
		const map = buildSdkAgentsMap();
		for (const def of Object.values(map)) {
			expect(def).not.toHaveProperty("advisorModel");
		}
	});
});
