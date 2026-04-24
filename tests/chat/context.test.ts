/**
 * Unit tests for system prompt assembly.
 *
 * `assembleSystemPrompt()` glues the five memory tiers into a single string
 * fed to the Agent SDK. These tests drive it with stub repositories — no
 * database, no embedding model — to cover the guard on empty memory, the
 * onboarding-detection branch, and the budget / filtering knobs.
 */

import { describe, expect, test } from "bun:test";
import { INITIAL_GOALS, INITIAL_PERSONA } from "../../src/chat/bootstrap.ts";
import { assembleSystemPrompt, type ContextDependencies } from "../../src/chat/context.ts";
import { ok, type Result } from "../../src/errors.ts";
import type { CoreMemoryRepository } from "../../src/memory/core.ts";
import type { EmbeddingService } from "../../src/memory/embeddings.ts";
import type { Node } from "../../src/memory/graph/types.ts";
import { asNodeId } from "../../src/memory/graph/types.ts";
import type { RetrievalService, SearchResult } from "../../src/memory/retrieval.ts";
import type { Skill, SkillRepository } from "../../src/memory/skills.ts";
import type { CoreMemorySlot, JsonValue } from "../../src/memory/types.ts";
import { SlotNotFoundError } from "../../src/memory/types.ts";
import type { UserModelDimension, UserModelRepository } from "../../src/memory/user_model.ts";
import { expectReject } from "../helpers.ts";

// ---------------------------------------------------------------------------
// Stubs
// ---------------------------------------------------------------------------

interface CoreSlots {
	readonly persona: JsonValue;
	readonly goals: JsonValue;
	readonly context: JsonValue;
}

const EMPTY_CORE: CoreSlots = { persona: {}, goals: {}, context: {} };

function stubCore(slots: Partial<CoreSlots> = {}): CoreMemoryRepository {
	const merged: CoreSlots = { ...EMPTY_CORE, ...slots };
	// Only readSlot is exercised by assembleSystemPrompt; other methods throw
	// so any unexpected call fails loudly during tests.
	const repo = {
		async readSlot(slot: CoreMemorySlot): Promise<Result<JsonValue, SlotNotFoundError>> {
			if (slot === "user_model") return ok({});
			return ok(merged[slot]);
		},
		async read() {
			throw new Error("stubCore.read should not be called");
		},
		async update() {
			throw new Error("stubCore.update should not be called");
		},
		async hash() {
			return "hash";
		},
	};
	return repo as unknown as CoreMemoryRepository;
}

function stubCoreFailing(): CoreMemoryRepository {
	const repo = {
		async readSlot() {
			return { ok: false as const, error: new SlotNotFoundError("persona") };
		},
		async read() {
			throw new Error("unused");
		},
		async update() {
			throw new Error("unused");
		},
		async hash() {
			return "hash";
		},
	};
	return repo as unknown as CoreMemoryRepository;
}

function stubUserModel(dims: readonly UserModelDimension[]): UserModelRepository {
	return {
		async getDimensions() {
			return dims;
		},
		async getDimension() {
			return null;
		},
		async updateDimension() {
			throw new Error("stubUserModel.updateDimension should not be called");
		},
	};
}

function stubRetrieval(results: readonly SearchResult[]): RetrievalService {
	const service = {
		async search() {
			return results;
		},
	};
	return service as unknown as RetrievalService;
}

function stubSkills(skills: readonly Skill[]): SkillRepository {
	return {
		async create() {
			throw new Error("stubSkills.create should not be called");
		},
		async findByTrigger() {
			return skills;
		},
		async recordOutcome() {
			throw new Error("stubSkills.recordOutcome should not be called");
		},
		async promote() {
			throw new Error("stubSkills.promote should not be called");
		},
		async getById() {
			return null;
		},
	};
}

function stubEmbeddings(): EmbeddingService {
	// Context assembly never calls the embedding service directly — retrieval
	// and skills already embed internally. Provide a throwing stub to flag any
	// accidental dependency.
	return {
		async embed() {
			throw new Error("stubEmbeddings.embed should not be called");
		},
		async embedBatch() {
			throw new Error("stubEmbeddings.embedBatch should not be called");
		},
		async warmup() {},
	};
}

function makeDim(name: string, value: JsonValue, confidence: number): UserModelDimension {
	return {
		id: 1,
		name,
		value,
		confidence,
		evidenceCount: 1,
		threshold: 5,
		egressSensitivity: "private",
		createdAt: new Date(0),
		updatedAt: new Date(0),
	};
}

function makeNode(id: number, kind: Node["kind"], body: string): Node {
	return {
		id: asNodeId(id),
		kind,
		body,
		embedding: null,
		trust: "owner",
		confidence: 1,
		importance: 0.5,
		sensitivity: "none",
		accessCount: 0,
		lastAccessedAt: null,
		createdAt: new Date(0),
		updatedAt: new Date(0),
		metadata: {},
		sourceEventId: null,
	};
}

function makeSearchResult(
	id: number,
	kind: Node["kind"],
	body: string,
	score: number,
): SearchResult {
	return {
		node: makeNode(id, kind, body),
		score,
		vectorRank: 1,
		ftsRank: 1,
		graphRank: null,
		recencyRank: null,
	};
}

function makeSkill(trigger: string, strategy: string, rate: number): Skill {
	return {
		id: 1,
		name: "s",
		trigger,
		strategy,
		successRate: rate,
		successCount: 0,
		attemptCount: 0,
		version: 1,
		parentId: null,
		promotedAt: null,
	};
}

function populatedDeps(overrides?: Partial<ContextDependencies>): ContextDependencies {
	return {
		coreMemory: stubCore({ persona: INITIAL_PERSONA, goals: INITIAL_GOALS }),
		userModel: stubUserModel([makeDim("communication_style", "direct", 0.85)]),
		retrieval: stubRetrieval([]),
		skills: stubSkills([]),
		embeddings: stubEmbeddings(),
		...overrides,
	};
}

// ---------------------------------------------------------------------------
// Guard: prompt length minimum
// ---------------------------------------------------------------------------

describe("assembleSystemPrompt guards", () => {
	test("empty memory still clears the 50-char floor via static Rules + Tools sections", async () => {
		// Pre-bootstrap scenario: persona/goals/context are `{}`, no user model,
		// no memories, no skills. The static Rules + Memory Tools sections in
		// buildPrompt() keep the prompt well above the guard. This test
		// documents that the guard is defensive insurance — under current
		// implementation it can only fire if buildPrompt() itself regresses.
		const deps: ContextDependencies = {
			coreMemory: stubCore(),
			userModel: stubUserModel([]),
			retrieval: stubRetrieval([]),
			skills: stubSkills([]),
			embeddings: stubEmbeddings(),
		};

		const prompt = await assembleSystemPrompt(deps, "hi");

		expect(prompt.length).toBeGreaterThanOrEqual(50);
		expect(prompt).toContain("# Rules");
		expect(prompt).toContain("# Your Tools");
	});

	test("error message format: guard mentions 'too short' and 'onboarding'", async () => {
		// Confirm the guard's message contract without being able to trigger it
		// through normal inputs. The source string is load-bearing — any future
		// reword needs to keep the "too short" / "onboarding" hints for the
		// operator.
		const src = await Bun.file("src/chat/context.ts").text();
		expect(src).toContain("System prompt too short");
		expect(src).toContain("Run onboarding first");
	});

	test("propagates SlotNotFoundError when a core slot is missing", async () => {
		const deps: ContextDependencies = {
			coreMemory: stubCoreFailing(),
			userModel: stubUserModel([]),
			retrieval: stubRetrieval([]),
			skills: stubSkills([]),
			embeddings: stubEmbeddings(),
		};

		await expectReject(
			() => assembleSystemPrompt(deps, "hi"),
			/Core memory slot not found: persona/,
		);
	});

	test("passes the guard with initial persona + goals seeds", async () => {
		const prompt = await assembleSystemPrompt(populatedDeps(), "hello");

		expect(prompt.length).toBeGreaterThanOrEqual(50);
		expect(prompt).toContain("# Identity");
		expect(prompt).toContain("# Current Goals");
	});
});

// ---------------------------------------------------------------------------
// Section population
// ---------------------------------------------------------------------------

describe("assembleSystemPrompt sections", () => {
	test("full assembly includes every section header when every tier has data", async () => {
		const deps = populatedDeps({
			coreMemory: stubCore({
				persona: INITIAL_PERSONA,
				goals: INITIAL_GOALS,
				context: { time: "morning" },
			}),
			userModel: stubUserModel([makeDim("communication_style", "direct and concise", 0.9)]),
			retrieval: stubRetrieval([
				makeSearchResult(1, "preference", "User prefers dark mode", 0.88),
				makeSearchResult(2, "fact", "Meeting at 10am Friday", 0.77),
			]),
			skills: stubSkills([makeSkill("greet owner", "Warm hello", 0.95)]),
		});

		const prompt = await assembleSystemPrompt(deps, "hi Theo");

		expect(prompt).toContain("# Identity");
		expect(prompt).toContain("# Current Goals");
		expect(prompt).toContain("# Owner Profile");
		expect(prompt).toContain("# Current Context");
		expect(prompt).toContain("# Relevant Memories");
		expect(prompt).toContain("# Active Skills");
		expect(prompt).toContain("- [preference] User prefers dark mode");
		expect(prompt).toContain("greet owner");
	});

	test("minimal memory: only persona + goals → prompt is short but passes guard", async () => {
		const prompt = await assembleSystemPrompt(populatedDeps(), "ping");

		expect(prompt).toContain("# Identity");
		expect(prompt).toContain("# Current Goals");
		// No core context slot and no memories/skills in populatedDeps.
		expect(prompt).not.toContain("# Current Context");
		expect(prompt).not.toContain("# Relevant Memories");
		expect(prompt).not.toContain("# Active Skills");
	});

	test("no skills: Active Skills section omitted entirely", async () => {
		const deps = populatedDeps({ skills: stubSkills([]) });
		const prompt = await assembleSystemPrompt(deps, "hi");
		expect(prompt).not.toContain("# Active Skills");
	});

	test("skills matching query: Active Skills section included with success rate", async () => {
		const deps = populatedDeps({
			skills: stubSkills([
				makeSkill("meeting prep", "summarize agenda", 0.85),
				makeSkill("email draft", "match tone", 0.78),
			]),
		});

		const prompt = await assembleSystemPrompt(deps, "prep for standup");

		expect(prompt).toContain("# Active Skills");
		expect(prompt).toContain("meeting prep");
		expect(prompt).toContain("(85% success)");
		expect(prompt).toContain("email draft");
	});

	test("RRF results included as memories with kind tags", async () => {
		const deps = populatedDeps({
			retrieval: stubRetrieval([
				makeSearchResult(1, "observation", "User seems focused today", 0.93),
			]),
		});

		const prompt = await assembleSystemPrompt(deps, "status?");

		expect(prompt).toContain("# Relevant Memories");
		expect(prompt).toContain("- [observation] User seems focused today");
	});
});

// ---------------------------------------------------------------------------
// Onboarding detection
// ---------------------------------------------------------------------------

describe("assembleSystemPrompt onboarding detection", () => {
	test("empty user model dimensions → onboarding preamble inserted", async () => {
		const deps = populatedDeps({ userModel: stubUserModel([]) });

		const prompt = await assembleSystemPrompt(deps, "hi");

		expect(prompt).toContain("# First Interaction");
		expect(prompt).toContain("first conversation");
	});

	test("any user model dimension → onboarding preamble absent", async () => {
		const deps = populatedDeps({
			userModel: stubUserModel([makeDim("humor", "dry", 0.5)]),
		});

		const prompt = await assembleSystemPrompt(deps, "hi");

		expect(prompt).not.toContain("# First Interaction");
	});
});

// ---------------------------------------------------------------------------
// Option budgets propagate to retrieval and skills
// ---------------------------------------------------------------------------

describe("assembleSystemPrompt option budgets", () => {
	test("memoryLimit is forwarded to retrieval.search", async () => {
		let capturedLimit: number | undefined;
		const retrieval = {
			async search(_q: string, opts?: { readonly limit?: number }) {
				capturedLimit = opts?.limit;
				return [];
			},
		} as unknown as RetrievalService;

		const deps = populatedDeps({ retrieval });
		await assembleSystemPrompt(deps, "hi", { memoryLimit: 3 });

		expect(capturedLimit).toBe(3);
	});

	test("skillLimit is forwarded to skills.findByTrigger", async () => {
		let capturedLimit: number | undefined;
		const skills: SkillRepository = {
			async create() {
				throw new Error("skills.create should not be called");
			},
			async findByTrigger(_q: string, limit: number) {
				capturedLimit = limit;
				return [];
			},
			async recordOutcome() {
				throw new Error("skills.recordOutcome should not be called");
			},
			async promote() {
				throw new Error("skills.promote should not be called");
			},
			async getById() {
				return null;
			},
		};

		const deps = populatedDeps({ skills });
		await assembleSystemPrompt(deps, "hi", { skillLimit: 2 });

		expect(capturedLimit).toBe(2);
	});

	test("defaults applied when options omitted (memoryLimit=15, skillLimit=5)", async () => {
		let memoryLimit: number | undefined;
		let skillLimit: number | undefined;

		const retrieval = {
			async search(_q: string, opts?: { readonly limit?: number }) {
				memoryLimit = opts?.limit;
				return [];
			},
		} as unknown as RetrievalService;

		const skillsRepo: SkillRepository = {
			async create() {
				throw new Error("skills.create should not be called");
			},
			async findByTrigger(_q: string, limit: number) {
				skillLimit = limit;
				return [];
			},
			async recordOutcome() {
				throw new Error("skills.recordOutcome should not be called");
			},
			async promote() {
				throw new Error("skills.promote should not be called");
			},
			async getById() {
				return null;
			},
		};

		const deps = populatedDeps({ retrieval, skills: skillsRepo });
		await assembleSystemPrompt(deps, "hi");

		expect(memoryLimit).toBe(15);
		expect(skillLimit).toBe(5);
	});
});

// ---------------------------------------------------------------------------
// Phase 13a: experimental-dimension filtering
// ---------------------------------------------------------------------------

describe("assembleSystemPrompt experimental-dimension filter (Phase 13a)", () => {
	function makeDimWithEvidence(
		name: string,
		evidenceCount: number,
		confidence: number,
	): UserModelDimension {
		return {
			id: 1,
			name,
			value: "placeholder value",
			confidence,
			evidenceCount,
			threshold: 10,
			egressSensitivity: "private",
			createdAt: new Date(0),
			updatedAt: new Date(0),
		};
	}

	test("Jungian dimension below the evidence floor is excluded from the prompt", async () => {
		const deps = populatedDeps({
			userModel: stubUserModel([
				makeDimWithEvidence("personality_type", 10, 0.5),
				makeDimWithEvidence("communication_style", 5, 0.9),
			]),
		});
		const prompt = await assembleSystemPrompt(deps, "hi");
		expect(prompt).not.toContain("personality_type");
		expect(prompt).toContain("communication_style");
	});

	test("Jungian dimension at the evidence floor is included", async () => {
		const deps = populatedDeps({
			userModel: stubUserModel([makeDimWithEvidence("archetypes", 50, 0.9)]),
		});
		const prompt = await assembleSystemPrompt(deps, "hi");
		expect(prompt).toContain("archetypes");
	});

	test("Big Five dimension with low evidence is still included", async () => {
		const deps = populatedDeps({
			userModel: stubUserModel([makeDimWithEvidence("openness", 1, 0.1)]),
		});
		const prompt = await assembleSystemPrompt(deps, "hi");
		expect(prompt).toContain("openness");
	});
});
