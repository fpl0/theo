/**
 * Subagent catalog.
 *
 * Eight cognitive modes that Theo can delegate to during a turn or run as
 * scheduled jobs. Every entry is an SDK `AgentDefinition`; tools are not
 * listed so subagents inherit the parent's full tool set (including the MCP
 * memory tools).
 *
 * Plan-then-execute subagents (`planner`, `coder`, `researcher`, `writer`,
 * `reflector`) carry an `advisorModel` — the dispatch call site passes
 * `options.settings.advisorModel` so the SDK enables the server-side advisor
 * tool (beta header `advisor-tool-2026-03-01`). Reflex-speed subagents
 * (`scanner`, `consolidator`) do not use the advisor.
 *
 * The keys here are the canonical subagent names used throughout Theo —
 * scheduled jobs reference them by name, and the main chat agent delegates
 * to them by name via the Agent tool.
 */

import type { AgentDefinition } from "@anthropic-ai/claude-agent-sdk";

/** The model a subagent uses when it also acts as its own advisor. */
const ADVISOR_OPUS = "claude-opus-4-6";

/**
 * Subagent name string literal — tightens references at the call sites.
 *
 * Eight scoped subagents (`coder`, `researcher`, `writer`, `planner`,
 * `psychologist`, `consolidator`, `reflector`, `scanner`) match the Phase 14
 * plan catalog. `main` is a ninth "generalist" entry used by the scheduler's
 * `goal-execution` builtin job — it represents a full Theo turn from inside
 * the scheduler, carrying the same advisor-assisted Sonnet+Opus pairing.
 */
export type SubagentName =
	| "main"
	| "coder"
	| "researcher"
	| "writer"
	| "planner"
	| "psychologist"
	| "consolidator"
	| "reflector"
	| "scanner";

/**
 * A Theo subagent definition. Mirrors SDK `AgentDefinition` plus an optional
 * `advisorModel` pulled out for the dispatch wiring to read without digging
 * into settings.
 *
 * `tools` is intentionally NOT declared — subagents inherit the parent's full
 * tool set, which includes the MCP memory tools.
 */
export interface TheoAgentDefinition extends AgentDefinition {
	/**
	 * When set, the dispatch call site (chat engine, scheduler, ideation job)
	 * passes this value as `options.settings.advisorModel` so the SDK enables
	 * the server-side advisor tool. Reflex-speed subagents leave this unset.
	 */
	readonly advisorModel?: string;
}

// ---------------------------------------------------------------------------
// Subagent catalog
// ---------------------------------------------------------------------------

export const SUBAGENTS: Readonly<Record<SubagentName, TheoAgentDefinition>> = {
	main: {
		description: "General-purpose Theo turn — owner-facing chat and scheduled goal execution",
		prompt:
			"You are Theo's main conversational agent. " +
			"Serve the owner with the full memory toolset — " +
			"search before answering, store what matters, and " +
			"keep the user model honest about what you have " +
			"observed. Delegate to a specialist subagent when " +
			"the task clearly fits one (coder, researcher, " +
			"writer, planner, psychologist); handle the rest " +
			"yourself with a first-person voice that treats the " +
			"owner as an equal.",
		model: "sonnet",
		maxTurns: 40,
		advisorModel: ADVISOR_OPUS,
	},

	coder: {
		description: "Write, edit, and debug code across any language or framework",
		prompt:
			"You are Theo's software engineering agent. " +
			"You have full access to file system tools and " +
			"memory. Write clean, tested code. Follow the " +
			"owner's coding conventions. When debugging, " +
			"trace the problem systematically before " +
			"attempting fixes.",
		model: "sonnet",
		maxTurns: 200,
		advisorModel: ADVISOR_OPUS,
	},

	researcher: {
		description: "Deep investigation — web search, doc reading, synthesis",
		prompt:
			"You are Theo's research agent. Your job is " +
			"deep investigation. Search the web, read " +
			"documentation, cross-reference sources, and " +
			"synthesize findings into clear summaries. " +
			"Always cite your sources. Distinguish between " +
			"facts and opinions.",
		model: "sonnet",
		maxTurns: 50,
		advisorModel: ADVISOR_OPUS,
	},

	writer: {
		description: "Draft emails, messages, and documents in the owner's voice",
		prompt:
			"You are Theo's writing agent. Draft in the " +
			"owner's voice — check the user model for " +
			"their communication style. Match their tone, " +
			"formality level, and typical phrasing. When " +
			"drafting email replies, reference any relevant " +
			"context from memory.",
		model: "sonnet",
		maxTurns: 20,
		advisorModel: ADVISOR_OPUS,
	},

	planner: {
		description: "Break complex goals into concrete steps and identify dependencies",
		prompt:
			"You are Theo's planning agent. Break down " +
			"complex goals into concrete, actionable " +
			"steps. Identify dependencies between steps. " +
			"Estimate effort where possible. Store the " +
			"plan in memory so it can be tracked over time.",
		model: "sonnet",
		maxTurns: 20,
		advisorModel: ADVISOR_OPUS,
	},

	psychologist: {
		description:
			"Jungian psychologist — tracks psychological profile, behavioral patterns, individuation",
		prompt:
			"You are Theo's depth psychology agent, " +
			"grounded in Jungian analytical psychology. " +
			"You observe behavioral patterns, track " +
			"psychological dimensions (personality type, " +
			"shadow patterns, dominant archetypes, " +
			"individuation markers), and update the user " +
			"model with evidence-based observations. You " +
			"never diagnose — you observe patterns and " +
			"note them with appropriate confidence levels. " +
			"Your insights help Theo interact with the " +
			"owner in a way that supports their growth " +
			"and wellbeing.",
		model: "opus",
		maxTurns: 30,
		// Psychologist already runs Opus; no advisor — it IS the advisor class.
	},

	consolidator: {
		description: "Compress and deduplicate memories",
		prompt:
			"You are Theo's memory maintenance agent. " +
			"Your job is to keep the knowledge graph " +
			"clean and efficient. Summarize old " +
			"conversations, merge duplicate knowledge " +
			"nodes, and ensure the graph doesn't grow " +
			"without bound. Preserve important nuance " +
			"when compressing — if in doubt, keep more " +
			"detail rather than less.",
		model: "haiku",
		maxTurns: 20,
		// Reflex-speed: no advisor.
	},

	reflector: {
		description: "Analyze behavioral patterns, calibrate self-model, and refine procedural skills",
		prompt:
			"You are Theo's self-reflection agent. " +
			"Review recent interactions and identify " +
			"patterns: What went well? What could " +
			"improve? Where was Theo's judgment accurate " +
			"vs inaccurate? Update the self-model " +
			"calibration for each domain. Look for " +
			"patterns the main agent might not notice — " +
			"recurring themes, blind spots, areas of " +
			"growth.\n\n" +
			"When you identify a recurring strategy that " +
			"works well, create or refine a procedural " +
			"skill using store_skill. When an existing " +
			"skill underperforms, create a refined version " +
			"with parent_id pointing to the predecessor. " +
			"Skills with success_rate > 0.85 and 20+ " +
			"attempts are candidates for promotion into " +
			"the persona.",
		model: "sonnet",
		maxTurns: 10,
		advisorModel: ADVISOR_OPUS,
	},

	scanner: {
		description: "Surface forgotten commitments and pending follow-ups",
		prompt:
			"You are Theo's proactive monitoring agent. " +
			"Search memory for any commitments, promises, " +
			"deadlines, or follow-ups that might have been " +
			"forgotten. Create notifications for anything " +
			"time-sensitive. Pay special attention to " +
			'phrases like "I\'ll do X by Y", ' +
			'"remind me", "don\'t forget", and similar ' +
			"commitment language.",
		model: "haiku",
		maxTurns: 10,
		// Reflex-speed: no advisor.
	},
};

/** All subagent names, in the order they appear in the catalog. */
export const SUBAGENT_NAMES: readonly SubagentName[] = [
	"main",
	"coder",
	"researcher",
	"writer",
	"planner",
	"psychologist",
	"consolidator",
	"reflector",
	"scanner",
];

/**
 * Strip the Theo-specific `advisorModel` field so the result is safe to pass
 * to the SDK as `options.agents[name]`. The SDK tolerates unknown keys, but
 * this keeps the surface clean and explicit.
 */
export function toSdkAgentDefinition(def: TheoAgentDefinition): AgentDefinition {
	const { advisorModel: _advisorModel, ...rest } = def;
	return rest;
}

/**
 * Build the `options.agents` map for the SDK from the subagent catalog.
 * Used by the chat engine, the scheduler, and any future dispatch site
 * that hands subagents to `query()`.
 */
export function buildSdkAgentsMap(
	catalog: Readonly<Record<string, TheoAgentDefinition>> = SUBAGENTS,
): Record<string, AgentDefinition> {
	const map: Record<string, AgentDefinition> = {};
	for (const [name, def] of Object.entries(catalog)) {
		map[name] = toSdkAgentDefinition(def);
	}
	return map;
}

// ---------------------------------------------------------------------------
// Scheduler adapter
// ---------------------------------------------------------------------------

/**
 * Shape the scheduler consumes for each subagent. The scheduler has its own
 * `SubagentDefinition` interface (`src/scheduler/types.ts`) with
 * `systemPromptPrefix` instead of a full prompt; this adapter maps the full
 * catalog into that shape so the scheduler can use Phase 14's subagents
 * directly without a second source of truth.
 *
 * Model name passthrough keeps the SDK alias handling centralized in the
 * dispatch layer — "sonnet" / "opus" / "haiku" resolve to the current
 * concrete model IDs at query time.
 */
export interface SchedulerFacingDefinition {
	readonly model: string;
	readonly maxTurns: number;
	readonly systemPromptPrefix: string;
	readonly advisorModel?: string;
}

/**
 * Project the full catalog into the scheduler's shape. The `prompt` becomes
 * the `systemPromptPrefix` — the scheduler wraps it with job-specific
 * instructions before each run.
 */
export function buildSchedulerSubagents(
	catalog: Readonly<Record<string, TheoAgentDefinition>> = SUBAGENTS,
): Record<string, SchedulerFacingDefinition> {
	const map: Record<string, SchedulerFacingDefinition> = {};
	for (const [name, def] of Object.entries(catalog)) {
		const base = {
			model: def.model ?? "sonnet",
			maxTurns: def.maxTurns ?? 20,
			systemPromptPrefix: def.prompt,
		};
		map[name] = def.advisorModel !== undefined ? { ...base, advisorModel: def.advisorModel } : base;
	}
	return map;
}
