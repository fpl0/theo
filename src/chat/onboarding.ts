/**
 * Onboarding: first-interaction detection and psychologist-led interview.
 *
 * Onboarding is NOT a separate system — it is a regular conversation with an
 * augmented system prompt that instructs Theo to delegate to the `psychologist`
 * subagent. The interview unfolds over three phases (narrative → structured
 * dimensions → working agreement) and uses the standard MCP memory tools to
 * seed the user model, core memory, and knowledge graph.
 *
 * Bootstrap (`bootstrap.ts`) exposes a lightweight onboarding preamble meant
 * for the main agent's first reply — "introduce yourself, ask something
 * open-ended". This module complements it: when the psychologist is invoked
 * during onboarding, this prompt drives the depth interview.
 *
 * Detection: the module prefers the async `shouldOnboard(repo)` signature
 * because it keeps the decision side-effect-free and testable; the synchronous
 * `shouldAugmentForOnboarding` in `bootstrap.ts` remains for callers that
 * already hold the dimensions array.
 */

import type { UserModelRepository } from "../memory/user_model.ts";

// ---------------------------------------------------------------------------
// Detection
// ---------------------------------------------------------------------------

/**
 * Return true when no user-model dimensions have been recorded yet.
 * A fresh Theo with a seeded persona but an empty user model should onboard.
 */
export async function shouldOnboard(repo: UserModelRepository): Promise<boolean> {
	const dimensions = await repo.getDimensions();
	return dimensions.length === 0;
}

// ---------------------------------------------------------------------------
// Interview prompt (for the psychologist subagent)
// ---------------------------------------------------------------------------

/**
 * The three-phase interview instructions. Prepended to the main agent's
 * system prompt when onboarding is detected; the main agent is expected to
 * delegate to the `psychologist` subagent, which inherits the instructions
 * through the shared system prompt and its own richer persona prompt.
 *
 * Phase markers are literal strings (`PHASE 1`, `PHASE 2`, `PHASE 3`) so
 * tests can assert on the structure without matching the full prose.
 */
export const ONBOARDING_INTERVIEW_PROMPT = `# Onboarding Interview

This is Theo's onboarding. Delegate to the \`psychologist\` subagent to
conduct a depth interview across three phases. The psychologist must use
the MCP memory tools (\`store_memory\`, \`update_user_model\`,
\`update_core\`) as the interview progresses — nothing here should be
free-floating text. When the three phases are complete, the main agent
resumes ordinary conversation.

PHASE 1 — NARRATIVE (spend most time here):
Ask open-ended questions that invite stories. Key prompts:
- "Tell me about a turning point in your life..."
- "What's a challenge you're proud of overcoming?"
- "Describe a typical day when everything goes well."
After each answer, use \`store_memory\` to save key facts and
observations. When you have gathered enough narrative context (at
least 4–5 exchanges), transition to Phase 2 by saying: "I'd like to
ask some more specific questions now."

PHASE 2 — STRUCTURED DIMENSIONS:
Ask targeted questions to fill these dimensions:
- Communication style: "Do you prefer direct feedback or diplomatic
  framing?"
- Energy patterns: "When's your peak productivity? Morning or evening?"
- Decision making: "Do you decide quickly on instinct, or do you
  deliberate?"
- Boundaries: "What topics are off-limits for me?"
- Interests: "What are you most passionate about right now?"
Use \`update_user_model\` for each dimension with evidence=1 (initial
observation; confidence builds over time). When all dimensions have
been addressed, transition to Phase 3 by saying: "Let's talk about
how we'll work together."

PHASE 3 — WORKING AGREEMENT:
- "How autonomous should I be? When should I act vs. ask?"
- "What does helpful look like to you? What does annoying look like?"
- "Is there anything you want me to always remember?"
Use \`update_core\` to set the \`persona\` and \`goals\` slots based on
the answers. Preserve any existing seed content — merge, do not
overwrite wholesale.

Tone: warm, unhurried, curious. Do not read the phase names aloud; the
owner should experience this as a natural conversation.`;

/**
 * Prefix for the augmented system prompt. The dispatch layer prepends this
 * block so the main agent sees the interview instructions in the stable
 * cache zone (ordered before per-turn content).
 */
export function augmentSystemPromptForOnboarding(baseSystemPrompt: string): string {
	return `${ONBOARDING_INTERVIEW_PROMPT}\n\n${baseSystemPrompt}`;
}
