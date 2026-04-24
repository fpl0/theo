/**
 * Subagent catalog.
 *
 * Cognitive modes the main chat agent delegates to via the Agent tool. Every
 * entry is an SDK `AgentDefinition`; tools are not listed so subagents
 * inherit the parent's full tool set (including the MCP memory tools).
 *
 * Plan-then-execute subagents carry an `advisorModel` — the dispatch call
 * site passes `options.settings.advisorModel` so the SDK enables the
 * server-side advisor tool (beta header `advisor-tool-2026-03-01`).
 */

import type { AgentDefinition } from "@anthropic-ai/claude-agent-sdk";

/** The model a subagent uses when it also acts as its own advisor. */
const ADVISOR_OPUS = "claude-opus-4-6";

export type SubagentName = "main" | "coder" | "researcher" | "writer" | "planner" | "psychologist";

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
	},
};

/**
 * Build the `settings` object forwarded to the SDK. Returns undefined when
 * no advisor is configured so callers can spread it under
 * `exactOptionalPropertyTypes` without an explicit-undefined leak.
 */
export function advisorSettings(model: string | undefined): { advisorModel: string } | undefined {
	return model === undefined ? undefined : { advisorModel: model };
}

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
