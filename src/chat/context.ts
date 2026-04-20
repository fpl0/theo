/**
 * System prompt assembly from Theo's memory tiers.
 *
 * The prompt is composed fresh per session from:
 *   1. Persona (never truncated) — who Theo is
 *   2. Goals (never truncated) — what Theo is working on
 *   3. User model — who the owner is, as observed
 *   4. Current context — recent activity, active tasks
 *   5. RRF search results — memories relevant to the incoming message
 *   6. Active skills — procedural knowledge matching the incoming message
 *
 * Ordering is stable-to-volatile for cache efficiency; `buildPrompt()` in
 * `prompt.ts` handles the section layout.
 *
 * The 50-character guard rejects empty memory states: the agent would have
 * no identity and no instructions. Run onboarding / bootstrap first.
 */

import type { CoreMemoryRepository } from "../memory/core.ts";
import type { EmbeddingService } from "../memory/embeddings.ts";
import type { RetrievalService } from "../memory/retrieval.ts";
import type { SkillRepository } from "../memory/skills.ts";
import type { JsonValue } from "../memory/types.ts";
import type { UserModelRepository } from "../memory/user_model.ts";
import { buildPrompt } from "./prompt.ts";

// ---------------------------------------------------------------------------
// Dependencies
// ---------------------------------------------------------------------------

/** Repositories required to assemble a system prompt. */
export interface ContextDependencies {
	readonly coreMemory: CoreMemoryRepository;
	readonly userModel: UserModelRepository;
	readonly retrieval: RetrievalService;
	readonly skills: SkillRepository;
	readonly embeddings: EmbeddingService;
}

// ---------------------------------------------------------------------------
// Options and thresholds
// ---------------------------------------------------------------------------

/** Options for system prompt assembly. */
export interface AssembleOptions {
	/** Maximum RRF results to include. Default 15. */
	readonly memoryLimit?: number;
	/** Maximum active skills to include. Default 5. */
	readonly skillLimit?: number;
}

/** Minimum prompt length — shorter means memory is empty. */
const MIN_PROMPT_LENGTH = 50;

/** Default memory retrieval limit. */
const DEFAULT_MEMORY_LIMIT = 15;

/** Default active skills limit. */
const DEFAULT_SKILL_LIMIT = 5;

// ---------------------------------------------------------------------------
// Assembly
// ---------------------------------------------------------------------------

/**
 * Build a complete system prompt from the memory tiers.
 *
 * Throws if any core memory slot is missing (database corruption — not a
 * runtime condition). Throws if the final prompt is under MIN_PROMPT_LENGTH
 * chars — memory is empty, run bootstrap first.
 */
export async function assembleSystemPrompt(
	deps: ContextDependencies,
	userMessage: string,
	options?: AssembleOptions,
): Promise<string> {
	const memoryLimit = options?.memoryLimit ?? DEFAULT_MEMORY_LIMIT;
	const skillLimit = options?.skillLimit ?? DEFAULT_SKILL_LIMIT;

	// Core memory slots are seeded by migration; a missing slot is data
	// corruption. Let Result errors propagate as thrown exceptions — the
	// engine converts them to turn.failed events upstream.
	const [personaResult, goalsResult, contextResult, userModel, memories, skills] =
		await Promise.all([
			deps.coreMemory.readSlot("persona"),
			deps.coreMemory.readSlot("goals"),
			deps.coreMemory.readSlot("context"),
			deps.userModel.getDimensions(),
			deps.retrieval.search(userMessage, { limit: memoryLimit }),
			deps.skills.findByTrigger(userMessage, skillLimit),
		]);

	if (!personaResult.ok) throw personaResult.error;
	if (!goalsResult.ok) throw goalsResult.error;
	if (!contextResult.ok) throw contextResult.error;

	const persona: JsonValue = personaResult.value;
	const goals: JsonValue = goalsResult.value;
	const context: JsonValue = contextResult.value;

	// Bootstrap seeds core memory but not user-model dimensions, so a freshly
	// bootstrapped Theo still triggers onboarding until the first
	// update_user_model tool call fires.
	const onboarding = userModel.length === 0;

	const prompt = buildPrompt(
		{
			persona,
			goals,
			userModel: userModel.map((d) => ({
				name: d.name,
				value: d.value,
				confidence: d.confidence,
			})),
			context,
			memories: memories.map((m) => ({
				body: m.node.body,
				score: m.score,
				kind: m.node.kind,
			})),
			skills: skills.map((s) => ({
				trigger: s.trigger,
				strategy: s.strategy,
				successRate: s.successRate,
			})),
		},
		{ onboarding },
	);

	if (prompt.length < MIN_PROMPT_LENGTH) {
		throw new Error(
			`System prompt too short (${String(prompt.length)} chars) — memory may be empty. ` +
				"Run onboarding first.",
		);
	}

	return prompt;
}
