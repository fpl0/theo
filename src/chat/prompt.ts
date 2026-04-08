/**
 * System prompt builder.
 *
 * Transforms structured data from memory tiers into a coherent system prompt.
 * Produces structured prose with section headers, not raw JSON dumps.
 *
 * Sections are ordered stable-to-volatile for cache efficiency:
 *   1. Identity (persona) — rarely changes
 *   2. Rules — static
 *   3. Tool Instructions — static
 *   4. Onboarding preamble — session-level, optional
 *   5. Active Skills — session-level
 *   6. Goals — session-level
 *   7. Owner Profile — session-level
 *   8. Context — changes every turn
 *   9. Memories — changes every turn
 *
 * Empty sections produce empty strings and are filtered out — no blank
 * headers appear in the final prompt.
 */

import type { JsonValue } from "../memory/types.ts";
import { ONBOARDING_PREAMBLE } from "./bootstrap.ts";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface PromptSources {
	readonly persona: JsonValue;
	readonly goals: JsonValue;
	readonly userModel: ReadonlyArray<{
		readonly name: string;
		readonly value: JsonValue;
		readonly confidence: number;
	}>;
	readonly context: JsonValue;
	readonly memories: ReadonlyArray<{
		readonly body: string;
		readonly score: number;
		readonly kind: string;
	}>;
	readonly skills?: ReadonlyArray<{
		readonly trigger: string;
		readonly strategy: string;
		readonly successRate: number;
	}>;
}

export interface PromptOptions {
	readonly onboarding?: boolean | undefined;
}

// ---------------------------------------------------------------------------
// Section Renderers
// ---------------------------------------------------------------------------

/**
 * Render the persona document into natural language instructions.
 * Performs runtime type narrowing on JsonValue since core memory
 * stores arbitrary JSONB — the structure is validated here, not
 * by the type system.
 */
export function renderPersona(persona: JsonValue): string {
	if (!persona || typeof persona !== "object" || Array.isArray(persona)) {
		return "";
	}

	const p = persona as Record<string, JsonValue>;
	if (Object.keys(p).length === 0) {
		return "";
	}

	const lines: string[] = [];

	lines.push("# Identity");
	lines.push("");

	if (p["name"]) {
		lines.push(`You are ${String(p["name"])}.`);
	}

	if (p["relationship"]) {
		lines.push(`${String(p["relationship"])}.`);
	}

	lines.push("");

	const voice = p["voice"];
	if (voice && typeof voice === "object" && !Array.isArray(voice)) {
		const v = voice as Record<string, JsonValue>;
		if (v["tone"]) {
			lines.push(`Your tone is ${String(v["tone"])}.`);
		}
		if (v["style"]) {
			lines.push(`${String(v["style"])}.`);
		}
		if (Array.isArray(v["avoids"])) {
			lines.push("");
			lines.push("Avoid:");
			for (const item of v["avoids"]) {
				lines.push(`- ${String(item)}`);
			}
		}
		if (Array.isArray(v["qualities"])) {
			lines.push("");
			for (const item of v["qualities"]) {
				lines.push(`- ${String(item)}`);
			}
		}
	}

	const autonomy = p["autonomy"];
	if (autonomy && typeof autonomy === "object" && !Array.isArray(autonomy)) {
		const a = autonomy as Record<string, JsonValue>;
		lines.push("");
		lines.push("## Autonomy");
		if (a["description"]) {
			lines.push(`Default: ${String(a["description"])}.`);
		}
		if (Array.isArray(a["levels"])) {
			for (const level of a["levels"]) {
				lines.push(`- ${String(level)}`);
			}
		}
	}

	if (p["memory_philosophy"]) {
		lines.push("");
		lines.push(`Memory philosophy: ${String(p["memory_philosophy"])}`);
	}

	return lines.join("\n");
}

/** Render behavioral rules. Static section — never changes. */
export function renderBehavioralRules(): string {
	return `# Rules

## Privacy
- Never share the owner's information with anyone.
  There is no "anyone" — Theo serves one person.
- Respect sensitivity levels on stored memories.
  Restricted data (financial, medical) and sensitive
  data (identity, location, relationship) require
  extra care.
- If the owner asks you to forget something,
  acknowledge it. You cannot delete events (they are
  immutable), but you can update core memory and mark
  knowledge nodes as superseded.

## Autonomy
- Follow the autonomy level set in your persona.
  When in doubt, suggest rather than act.
- If you're about to do something irreversible,
  always confirm first regardless of autonomy level.
- Proactive suggestions are welcome. Proactive
  irreversible actions are not.

## Accuracy
- Never fabricate memories. If you don't remember
  something, say so. Search your memory before
  claiming you don't know.
- Distinguish between what you know (stored in
  memory), what you infer (pattern matching), and
  what you're guessing (no basis). Be explicit about
  which one you're doing.
- When your confidence in a user model dimension is
  low, treat it as a hypothesis, not a fact.

## Continuity
- Reference past conversations naturally when
  relevant. Don't force it — mention previous context
  when it genuinely helps.
- Track commitments. If the owner said they'd do
  something, or asked you to remind them, follow up
  at appropriate times.
- Notice patterns over time. If the owner
  consistently does X, that's worth noting in the
  user model even if they never explicitly stated a
  preference.

## Corrections
- When the owner corrects you, accept it immediately.
  Owner corrections are authoritative — update the
  relevant memory and user model dimension right away.
- Never argue with a correction or double down on a
  retrieved memory that the owner says is wrong.
  People change, and the owner knows themselves better
  than your model does.

## Boundaries
- You are a capable agent, not a therapist or
  companion. When the owner needs emotional support
  beyond your scope, acknowledge their feelings
  honestly and suggest appropriate resources.
- Be warm and genuine, but do not simulate emotional
  intimacy or encourage dependency.

## Error Handling
- If a tool call fails, tell the owner what happened
  and what you'll do about it. Don't silently retry
  and pretend nothing went wrong.
- If you're unsure how to proceed, say so. Asking
  for clarification is always better than guessing
  wrong.`;
}

/** Render tool usage instructions. Static section — never changes. */
export function renderToolInstructions(): string {
	return `# Memory Tools

You have access to persistent memory through MCP tools.
Use them actively — your memory is your advantage over
stateless assistants.

**store_memory** — Save important facts, preferences,
observations, and commitments. Store liberally as
atomic, specific facts — prefer "prefers dark mode in
VS Code" over "prefers dark mode everywhere." The
consolidation system finds patterns across individual
memories over time. Include context about why something
matters, not just what it is.

**search_memory** — Search your knowledge graph before
answering questions about the owner, their preferences,
past conversations, or commitments. If you're unsure
whether you know something, search first.

**read_core** — Read your core memory slots (persona,
goals, user_model, context) to refresh your understanding.
Rarely needed during conversation since these are already
in your system prompt.

**update_core** — Update your persona, goals, or context
when you learn something that changes your operating
parameters. Use sparingly and deliberately — core memory
is your identity, not a scratchpad.

**update_user_model** — Update a dimension of the owner's
profile when you observe behavioral patterns. Include
evidence count and appropriate confidence. A single
observation is evidence=1 with low confidence. Consistent
patterns across many interactions warrant higher
confidence. When you observe something significant, often
both tools apply: store_memory for the specific fact,
update_user_model for the structured pattern.`;
}

/** Render active skills as one-line summaries. Strategy truncated to 120 chars. */
export function renderActiveSkills(
	skills?: ReadonlyArray<{
		readonly trigger: string;
		readonly strategy: string;
		readonly successRate: number;
	}>,
): string {
	if (!skills || skills.length === 0) {
		return "";
	}

	const lines: string[] = [];
	lines.push("# Active Skills");
	lines.push("");

	for (const skill of skills) {
		const rate = (skill.successRate * 100).toFixed(0);
		lines.push(`- **${skill.trigger}** (${rate}% success): ${skill.strategy.slice(0, 120)}`);
	}

	return lines.join("\n");
}

/** Render goals with status annotations. */
export function renderGoals(goals: JsonValue): string {
	if (!goals || typeof goals !== "object" || Array.isArray(goals)) {
		return "";
	}

	const g = goals as Record<string, JsonValue>;
	if (Object.keys(g).length === 0) {
		return "";
	}

	const lines: string[] = [];
	lines.push("# Current Goals");
	lines.push("");

	for (const [priority, goal] of Object.entries(g)) {
		if (goal && typeof goal === "object" && !Array.isArray(goal)) {
			const gObj = goal as Record<string, JsonValue>;
			const status = gObj["status"] ? ` [${String(gObj["status"])}]` : "";
			lines.push(`**${priority}**${status}: ${String(gObj["description"] ?? "")}`);
			if (gObj["rationale"]) {
				lines.push(`  ${String(gObj["rationale"])}`);
			}
		}
	}

	return lines.join("\n");
}

/**
 * Render the owner's profile with confidence annotations.
 * Empty dimensions produce a "No profile yet" message so the agent
 * knows it is meeting the owner for the first time.
 */
export function renderUserModel(
	dimensions: ReadonlyArray<{
		readonly name: string;
		readonly value: JsonValue;
		readonly confidence: number;
	}>,
): string {
	if (dimensions.length === 0) {
		return "# Owner Profile\n\nNo profile yet. You are meeting the owner for the first time.";
	}

	const lines: string[] = [];
	lines.push("# Owner Profile");
	lines.push("");

	for (const dim of dimensions) {
		const confidence = dim.confidence >= 0.8 ? "high" : dim.confidence >= 0.5 ? "moderate" : "low";
		const valueStr = typeof dim.value === "string" ? dim.value : JSON.stringify(dim.value);
		lines.push(`- **${dim.name}** (${confidence} confidence): ${valueStr}`);
	}

	return lines.join("\n");
}

/** Render current context as a bullet list. Empty context produces empty string. */
export function renderContext(context: JsonValue): string {
	if (!context || typeof context !== "object" || Array.isArray(context)) {
		return "";
	}

	const c = context as Record<string, JsonValue>;
	if (Object.keys(c).length === 0) {
		return "";
	}

	const lines: string[] = [];
	lines.push("# Current Context");
	lines.push("");

	for (const [key, value] of Object.entries(c)) {
		const valueStr = typeof value === "string" ? value : JSON.stringify(value);
		lines.push(`- **${key}**: ${valueStr}`);
	}

	return lines.join("\n");
}

/** Render RRF search results with kind annotations. */
export function renderMemories(
	memories: ReadonlyArray<{
		readonly body: string;
		readonly score: number;
		readonly kind: string;
	}>,
): string {
	if (memories.length === 0) {
		return "";
	}

	const lines: string[] = [];
	lines.push("# Relevant Memories");
	lines.push("");

	for (const mem of memories) {
		lines.push(`- [${mem.kind}] ${mem.body}`);
	}

	return lines.join("\n");
}

// ---------------------------------------------------------------------------
// Prompt Builder
// ---------------------------------------------------------------------------

/**
 * Assemble a complete system prompt from memory tiers.
 *
 * Section order: stable → volatile for cache efficiency.
 * Empty sections are filtered — no blank headers in the output.
 */
export function buildPrompt(sources: PromptSources, options?: PromptOptions): string {
	const sections: string[] = [];

	// === CACHE ZONE 1: Stable (rarely changes) ===
	sections.push(renderPersona(sources.persona));
	sections.push(renderBehavioralRules());
	sections.push(renderToolInstructions());

	// === CACHE ZONE 2: Semi-stable (session-level changes) ===
	if (options?.onboarding === true) {
		sections.push(ONBOARDING_PREAMBLE);
	}
	sections.push(renderActiveSkills(sources.skills));
	sections.push(renderGoals(sources.goals));
	sections.push(renderUserModel(sources.userModel));

	// === CACHE ZONE 3: Volatile (changes every turn) ===
	sections.push(renderContext(sources.context));
	sections.push(renderMemories(sources.memories));

	return sections.filter((s) => s.length > 0).join("\n\n");
}
