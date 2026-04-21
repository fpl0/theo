/**
 * Subagent catalog validation.
 *
 * The catalog is mostly static — these tests pin the shape so accidental
 * edits (missing prompt, silent model rename, tool leakage) fail loudly.
 */

import { describe, expect, test } from "bun:test";
import {
	buildSchedulerSubagents,
	buildSdkAgentsMap,
	SUBAGENT_NAMES,
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
	"reflector",
]);

describe("SUBAGENTS catalog", () => {
	test("all scoped subagents plus the main generalist are defined", () => {
		const names = Object.keys(SUBAGENTS).sort();
		expect(names).toEqual(
			[
				"coder",
				"consolidator",
				"main",
				"planner",
				"psychologist",
				"reflector",
				"researcher",
				"scanner",
				"writer",
			].sort(),
		);
	});

	test("includes the eight Phase 14 catalog subagents", () => {
		const phase14 = [
			"coder",
			"consolidator",
			"planner",
			"psychologist",
			"reflector",
			"researcher",
			"scanner",
			"writer",
		] as const;
		for (const name of phase14) {
			expect(SUBAGENTS).toHaveProperty(name);
		}
	});

	test("SUBAGENT_NAMES lists every catalog key", () => {
		const catalogKeys = new Set(Object.keys(SUBAGENTS));
		expect(SUBAGENT_NAMES.length).toBe(catalogKeys.size);
		for (const name of SUBAGENT_NAMES) {
			expect(catalogKeys.has(name)).toBe(true);
		}
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

	test("reflex-speed subagents omit advisorModel", () => {
		expect(SUBAGENTS.scanner.advisorModel).toBeUndefined();
		expect(SUBAGENTS.consolidator.advisorModel).toBeUndefined();
		// Psychologist already runs Opus — no separate advisor.
		expect(SUBAGENTS.psychologist.advisorModel).toBeUndefined();
	});

	test("advisorModel is the Opus 4.6 identifier when set", () => {
		for (const name of ADVISOR_ELIGIBLE) {
			expect(SUBAGENTS[name].advisorModel).toBe("claude-opus-4-6");
		}
	});

	test("reflector prompt instructs skill creation and refinement", () => {
		const prompt = SUBAGENTS.reflector.prompt;
		expect(prompt).toContain("store_skill");
		expect(prompt).toContain("parent_id");
		expect(prompt).toContain("promot");
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
		// Core fields are preserved
		expect(sdk.model).toBe(SUBAGENTS.planner.model);
		expect(sdk.prompt).toBe(SUBAGENTS.planner.prompt);
		expect(sdk.description).toBe(SUBAGENTS.planner.description);
	});

	test("leaves reflex-speed subagents structurally unchanged", () => {
		const sdk = toSdkAgentDefinition(SUBAGENTS.scanner);
		expect(sdk).not.toHaveProperty("advisorModel");
		expect(sdk.model).toBe("haiku");
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

describe("buildSchedulerSubagents", () => {
	test("maps every subagent into scheduler-facing shape", () => {
		const map = buildSchedulerSubagents();
		for (const name of SUBAGENT_NAMES) {
			const entry = map[name];
			expect(entry).toBeDefined();
			const e = entry;
			if (!e) throw new Error("unreachable: entry must exist");
			expect(typeof e.model).toBe("string");
			expect(e.maxTurns).toBeGreaterThan(0);
			expect(e.systemPromptPrefix.length).toBeGreaterThan(0);
		}
	});

	test("preserves advisorModel when present on catalog entry", () => {
		const map = buildSchedulerSubagents();
		expect(map["planner"]?.advisorModel).toBe("claude-opus-4-6");
		expect(map["scanner"]?.advisorModel).toBeUndefined();
	});
});
