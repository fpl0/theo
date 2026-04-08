/**
 * Unit tests for the system prompt builder.
 *
 * Tests section rendering, ordering, filtering of empty sections,
 * confidence thresholds, onboarding preamble insertion, and cache
 * zone ordering.
 *
 * Pure unit tests — no database, no mocks needed.
 */

import { describe, expect, test } from "bun:test";
import { INITIAL_GOALS, INITIAL_PERSONA } from "../../src/chat/bootstrap.ts";
import {
	buildPrompt,
	type PromptSources,
	renderActiveSkills,
	renderBehavioralRules,
	renderContext,
	renderGoals,
	renderMemories,
	renderPersona,
	renderToolInstructions,
	renderUserModel,
} from "../../src/chat/prompt.ts";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Minimal empty sources for building prompts with specific overrides. */
function emptySources(overrides?: Partial<PromptSources>): PromptSources {
	return {
		persona: {},
		goals: {},
		userModel: [],
		context: {},
		memories: [],
		...overrides,
	};
}

// ---------------------------------------------------------------------------
// renderPersona
// ---------------------------------------------------------------------------

describe("renderPersona", () => {
	test("empty object produces empty string", () => {
		expect(renderPersona({})).toBe("");
	});

	test("null produces empty string", () => {
		expect(renderPersona(null)).toBe("");
	});

	test("array produces empty string", () => {
		expect(renderPersona([])).toBe("");
	});

	test("string produces empty string", () => {
		expect(renderPersona("not an object")).toBe("");
	});

	test("partial persona without voice or autonomy renders name and relationship only", () => {
		const result = renderPersona({ name: "Theo", relationship: "assistant" });

		expect(result).toContain("# Identity");
		expect(result).toContain("You are Theo.");
		expect(result).toContain("assistant.");
		expect(result).not.toContain("## Autonomy");
		expect(result).not.toContain("Avoid:");
		expect(result).not.toContain("Memory philosophy:");
	});

	test("populated persona renders natural language", () => {
		const result = renderPersona(INITIAL_PERSONA);

		expect(result).toContain("# Identity");
		expect(result).toContain("You are Theo.");
		expect(result).toContain(
			"personal AI agent — loyal to one person, built for decades of continuous use.",
		);
		expect(result).toContain("Your tone is warm, direct, confident.");
		expect(result).toContain(
			"first-person singular, never third person, never exposes internal process.",
		);
		expect(result).toContain("Avoid:");
		expect(result).toContain("- corporate assistant phrases");
		expect(result).toContain("## Autonomy");
		expect(result).toContain("Default:");
		expect(result).toContain("- observe");
		expect(result).toContain("- suggest");
		expect(result).toContain("- act");
		expect(result).toContain("- silent");
		expect(result).toContain("Memory philosophy:");
	});
});

// ---------------------------------------------------------------------------
// renderGoals
// ---------------------------------------------------------------------------

describe("renderGoals", () => {
	test("empty object produces empty string", () => {
		expect(renderGoals({})).toBe("");
	});

	test("null produces empty string", () => {
		expect(renderGoals(null)).toBe("");
	});

	test("array produces empty string", () => {
		expect(renderGoals([])).toBe("");
	});

	test("populated goals renders with status annotations", () => {
		const result = renderGoals(INITIAL_GOALS);

		expect(result).toContain("# Current Goals");
		expect(result).toContain("**primary** [pending]:");
		expect(result).toContain("Complete onboarding");
		expect(result).toContain("**secondary** [ongoing]:");
		expect(result).toContain("**tertiary** [ongoing]:");
	});
});

// ---------------------------------------------------------------------------
// renderUserModel
// ---------------------------------------------------------------------------

describe("renderUserModel", () => {
	test("empty dimensions shows 'No profile yet' message", () => {
		const result = renderUserModel([]);

		expect(result).toContain("# Owner Profile");
		expect(result).toContain("No profile yet. You are meeting the owner for the first time.");
	});

	test("dimensions rendered with confidence annotations", () => {
		const dimensions = [
			{ name: "communication_style", value: "direct and concise", confidence: 0.85 },
			{ name: "technical_level", value: "expert", confidence: 0.6 },
			{ name: "humor", value: "dry", confidence: 0.3 },
		];

		const result = renderUserModel(dimensions);

		expect(result).toContain("# Owner Profile");
		expect(result).toContain("**communication_style** (high confidence): direct and concise");
		expect(result).toContain("**technical_level** (moderate confidence): expert");
		expect(result).toContain("**humor** (low confidence): dry");
	});

	test("confidence threshold: 0.8 is high", () => {
		const result = renderUserModel([{ name: "test", value: "val", confidence: 0.8 }]);
		expect(result).toContain("(high confidence)");
	});

	test("confidence threshold: 0.79 is moderate", () => {
		const result = renderUserModel([{ name: "test", value: "val", confidence: 0.79 }]);
		expect(result).toContain("(moderate confidence)");
	});

	test("confidence threshold: 0.5 is moderate", () => {
		const result = renderUserModel([{ name: "test", value: "val", confidence: 0.5 }]);
		expect(result).toContain("(moderate confidence)");
	});

	test("confidence threshold: 0.49 is low", () => {
		const result = renderUserModel([{ name: "test", value: "val", confidence: 0.49 }]);
		expect(result).toContain("(low confidence)");
	});

	test("non-string values are JSON-stringified", () => {
		const result = renderUserModel([
			{ name: "interests", value: ["coding", "music"], confidence: 0.7 },
		]);
		expect(result).toContain('["coding","music"]');
	});
});

// ---------------------------------------------------------------------------
// renderContext
// ---------------------------------------------------------------------------

describe("renderContext", () => {
	test("empty object produces empty string", () => {
		expect(renderContext({})).toBe("");
	});

	test("null produces empty string", () => {
		expect(renderContext(null)).toBe("");
	});

	test("populated context renders as bullet list", () => {
		const result = renderContext({
			time: "2026-04-08T10:00:00Z",
			location: "Lisbon",
		});

		expect(result).toContain("# Current Context");
		expect(result).toContain("- **time**: 2026-04-08T10:00:00Z");
		expect(result).toContain("- **location**: Lisbon");
	});

	test("non-string values are JSON-stringified", () => {
		const result = renderContext({ count: 42 });
		expect(result).toContain("- **count**: 42");
	});
});

// ---------------------------------------------------------------------------
// renderMemories
// ---------------------------------------------------------------------------

describe("renderMemories", () => {
	test("empty array produces empty string", () => {
		expect(renderMemories([])).toBe("");
	});

	test("memories rendered with kind tags", () => {
		const memories = [
			{ body: "User prefers dark mode", score: 0.95, kind: "preference" },
			{ body: "Meeting scheduled for Friday", score: 0.88, kind: "fact" },
			{ body: "User seems stressed about deadlines", score: 0.72, kind: "observation" },
		];

		const result = renderMemories(memories);

		expect(result).toContain("# Relevant Memories");
		expect(result).toContain("- [preference] User prefers dark mode");
		expect(result).toContain("- [fact] Meeting scheduled for Friday");
		expect(result).toContain("- [observation] User seems stressed about deadlines");
	});
});

// ---------------------------------------------------------------------------
// renderToolInstructions
// ---------------------------------------------------------------------------

describe("renderToolInstructions", () => {
	test("contains all 5 tool names", () => {
		const result = renderToolInstructions();

		expect(result).toContain("# Memory Tools");
		expect(result).toContain("**store_memory**");
		expect(result).toContain("**search_memory**");
		expect(result).toContain("**read_core**");
		expect(result).toContain("**update_core**");
		expect(result).toContain("**update_user_model**");
	});
});

// ---------------------------------------------------------------------------
// renderBehavioralRules
// ---------------------------------------------------------------------------

describe("renderBehavioralRules", () => {
	test("contains all rule sections", () => {
		const result = renderBehavioralRules();

		expect(result).toContain("# Rules");
		expect(result).toContain("## Privacy");
		expect(result).toContain("## Autonomy");
		expect(result).toContain("## Accuracy");
		expect(result).toContain("## Continuity");
		expect(result).toContain("## Corrections");
		expect(result).toContain("## Boundaries");
		expect(result).toContain("## Error Handling");
	});
});

// ---------------------------------------------------------------------------
// renderActiveSkills
// ---------------------------------------------------------------------------

describe("renderActiveSkills", () => {
	test("undefined produces empty string", () => {
		expect(renderActiveSkills(undefined)).toBe("");
	});

	test("empty array produces empty string", () => {
		expect(renderActiveSkills([])).toBe("");
	});

	test("skills rendered as one-line summaries", () => {
		const skills = [
			{ trigger: "code review", strategy: "Analyze diff, check patterns", successRate: 0.92 },
			{ trigger: "meeting prep", strategy: "Summarize agenda, gather context", successRate: 0.85 },
			{ trigger: "email draft", strategy: "Match owner tone, be concise", successRate: 0.78 },
		];

		const result = renderActiveSkills(skills);

		expect(result).toContain("# Active Skills");
		expect(result).toContain("**code review** (92% success): Analyze diff, check patterns");
		expect(result).toContain("**meeting prep** (85% success): Summarize agenda, gather context");
		expect(result).toContain("**email draft** (78% success): Match owner tone, be concise");
	});

	test("strategy truncated to 120 chars", () => {
		const longStrategy = "A".repeat(200);
		const skills = [{ trigger: "test", strategy: longStrategy, successRate: 1.0 }];

		const result = renderActiveSkills(skills);

		// Strategy in output should be exactly 120 chars
		expect(result).toContain(`**test** (100% success): ${"A".repeat(120)}`);
		expect(result).not.toContain("A".repeat(121));
	});
});

// ---------------------------------------------------------------------------
// buildPrompt
// ---------------------------------------------------------------------------

describe("buildPrompt", () => {
	test("full prompt with all sources populated contains all section headers in order", () => {
		const sources: PromptSources = {
			persona: INITIAL_PERSONA,
			goals: INITIAL_GOALS,
			userModel: [{ name: "style", value: "direct", confidence: 0.8 }],
			context: { time: "morning" },
			memories: [{ body: "User likes coffee", score: 0.9, kind: "preference" }],
			skills: [{ trigger: "greet", strategy: "Be warm", successRate: 0.95 }],
		};

		const prompt = buildPrompt(sources);

		// All sections present
		expect(prompt).toContain("# Identity");
		expect(prompt).toContain("# Rules");
		expect(prompt).toContain("# Memory Tools");
		expect(prompt).toContain("# Active Skills");
		expect(prompt).toContain("# Current Goals");
		expect(prompt).toContain("# Owner Profile");
		expect(prompt).toContain("# Current Context");
		expect(prompt).toContain("# Relevant Memories");

		// Verify stable -> volatile ordering
		const identityPos = prompt.indexOf("# Identity");
		const rulesPos = prompt.indexOf("# Rules");
		const toolsPos = prompt.indexOf("# Memory Tools");
		const skillsPos = prompt.indexOf("# Active Skills");
		const goalsPos = prompt.indexOf("# Current Goals");
		const profilePos = prompt.indexOf("# Owner Profile");
		const contextPos = prompt.indexOf("# Current Context");
		const memoriesPos = prompt.indexOf("# Relevant Memories");

		expect(identityPos).toBeLessThan(rulesPos);
		expect(rulesPos).toBeLessThan(toolsPos);
		expect(toolsPos).toBeLessThan(skillsPos);
		expect(skillsPos).toBeLessThan(goalsPos);
		expect(goalsPos).toBeLessThan(profilePos);
		expect(profilePos).toBeLessThan(contextPos);
		expect(contextPos).toBeLessThan(memoriesPos);
	});

	test("empty persona produces no Identity section", () => {
		const prompt = buildPrompt(emptySources());

		expect(prompt).not.toContain("# Identity");
	});

	test("empty goals produces no Goals section", () => {
		const prompt = buildPrompt(emptySources());

		expect(prompt).not.toContain("# Current Goals");
	});

	test("empty context produces no Context section", () => {
		const prompt = buildPrompt(emptySources());

		expect(prompt).not.toContain("# Current Context");
	});

	test("no memories produces no Memories section", () => {
		const prompt = buildPrompt(emptySources());

		expect(prompt).not.toContain("# Relevant Memories");
	});

	test("no skills produces no Skills section", () => {
		const prompt = buildPrompt(emptySources());

		expect(prompt).not.toContain("# Active Skills");
	});

	test("section filtering: no double blank lines from empty sections", () => {
		const prompt = buildPrompt(emptySources());

		expect(prompt).not.toContain("\n\n\n");
	});

	test("onboarding preamble present when onboarding=true", () => {
		const prompt = buildPrompt(emptySources({ persona: INITIAL_PERSONA }), { onboarding: true });

		expect(prompt).toContain("# First Interaction");
		expect(prompt).toContain("first conversation with the owner");
	});

	test("onboarding preamble absent when onboarding=false", () => {
		const prompt = buildPrompt(emptySources({ persona: INITIAL_PERSONA }), { onboarding: false });

		expect(prompt).not.toContain("# First Interaction");
	});

	test("onboarding preamble absent by default", () => {
		const prompt = buildPrompt(emptySources({ persona: INITIAL_PERSONA }));

		expect(prompt).not.toContain("# First Interaction");
	});

	test("onboarding preamble appears between Identity and Goals", () => {
		const sources: PromptSources = {
			persona: INITIAL_PERSONA,
			goals: INITIAL_GOALS,
			userModel: [],
			context: {},
			memories: [],
		};

		const prompt = buildPrompt(sources, { onboarding: true });

		const identityPos = prompt.indexOf("# Identity");
		const preamblePos = prompt.indexOf("# First Interaction");
		const goalsPos = prompt.indexOf("# Current Goals");

		expect(identityPos).toBeLessThan(preamblePos);
		expect(preamblePos).toBeLessThan(goalsPos);
	});

	test("prompt with initial seeds exceeds 50 characters", () => {
		const sources: PromptSources = {
			persona: INITIAL_PERSONA,
			goals: INITIAL_GOALS,
			userModel: [],
			context: {},
			memories: [],
		};

		const prompt = buildPrompt(sources);

		expect(prompt.length).toBeGreaterThan(50);
	});

	test("cache zone ordering: Identity before Rules before Tools before Skills before Owner Profile", () => {
		const sources: PromptSources = {
			persona: INITIAL_PERSONA,
			goals: INITIAL_GOALS,
			userModel: [{ name: "style", value: "casual", confidence: 0.9 }],
			context: {},
			memories: [],
			skills: [{ trigger: "greet", strategy: "Be warm", successRate: 0.95 }],
		};

		const prompt = buildPrompt(sources);

		const identityPos = prompt.indexOf("# Identity");
		const rulesPos = prompt.indexOf("# Rules");
		const toolsPos = prompt.indexOf("# Memory Tools");
		const skillsPos = prompt.indexOf("# Active Skills");
		const profilePos = prompt.indexOf("# Owner Profile");

		expect(identityPos).toBeLessThan(rulesPos);
		expect(rulesPos).toBeLessThan(toolsPos);
		expect(toolsPos).toBeLessThan(skillsPos);
		expect(skillsPos).toBeLessThan(profilePos);
	});
});
