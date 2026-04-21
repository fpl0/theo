/**
 * Episode salience scoring.
 *
 * Computes an importance score in [0, 1] from a small set of heuristic
 * signals collected during an agent turn. The score is written to
 * `episode.importance` at creation time and used by the background
 * consolidator as a gate: episodes with importance >= CONSOLIDATION_GATE
 * are preserved at full fidelity indefinitely.
 *
 * The heuristic intentionally stays simple and deterministic. It is not
 * meant to be a final arbiter -- the agent can also call
 * `update_episode_importance` (Phase 9+) to override the heuristic when
 * it has direct evidence (e.g., the owner said "remember this").
 *
 * Signals (all independent, additive, capped at 1.0):
 *   - knowledgeNodesExtracted >= 3 : +0.15 (rich conversation)
 *   - coreMemoryUpdated            : +0.20 (core memory is high signal)
 *   - contradictionDetected        : +0.10 (conflict is important)
 *   - userExplicitMarker           : +0.25 (explicit owner intent)
 *
 * Base score is 0.5 (neutral). The thresholds were chosen so that a
 * single strong signal (user marker, core update) nudges the episode
 * above default without crossing the preservation gate (0.8). Two or
 * more strong signals reliably cross the gate.
 */

import type { NodeKind } from "../events/types.ts";

// ---------------------------------------------------------------------------
// Tunables (exported so tests can pin exact expectations)
// ---------------------------------------------------------------------------

/** Neutral starting score for an episode with no salience signals. */
export const BASE_SCORE = 0.5;

/** Score contributions for each signal. Tuned per Phase 13a design doc. */
export const SIGNAL_WEIGHTS = {
	knowledgeNodesExtracted: 0.15,
	coreMemoryUpdated: 0.2,
	contradictionDetected: 0.1,
	userExplicitMarker: 0.25,
} as const;

/** Nodes-extracted threshold below which the signal does not fire. */
export const KNOWLEDGE_NODES_THRESHOLD = 3;

/**
 * Consolidation gate. Episodes with `importance >= CONSOLIDATION_GATE`
 * are exempt from the automatic compressor. Placed at 0.8 so that a
 * single user marker + a core update (0.5 + 0.25 + 0.2 = 0.95) crosses
 * it reliably, while a lone knowledge-extraction bump (0.65) does not.
 */
export const CONSOLIDATION_GATE = 0.8;

// ---------------------------------------------------------------------------
// Signal shape
// ---------------------------------------------------------------------------

/**
 * Structured signals collected by the engine during (or at the end of)
 * a turn. All fields are required so the caller is forced to be
 * explicit about unknowns -- pass `false` / `0` rather than omitting.
 */
export interface SalienceSignals {
	/** How many knowledge nodes were extracted from this episode. */
	readonly knowledgeNodesExtracted: number;
	/** Did the agent mutate any of the four core memory slots? */
	readonly coreMemoryUpdated: boolean;
	/** Was a contradiction detected against existing memory? */
	readonly contradictionDetected: boolean;
	/** Did the owner use a phrase like "remember this" / "important"? */
	readonly userExplicitMarker: boolean;
}

// ---------------------------------------------------------------------------
// Scoring
// ---------------------------------------------------------------------------

/**
 * Score an episode's importance from heuristic signals.
 * Returns a value in [0, 1]; the result is deterministic and side-effect free.
 */
export function scoreEpisodeImportance(signals: SalienceSignals): number {
	let score = BASE_SCORE;
	if (signals.knowledgeNodesExtracted >= KNOWLEDGE_NODES_THRESHOLD) {
		score += SIGNAL_WEIGHTS.knowledgeNodesExtracted;
	}
	if (signals.coreMemoryUpdated) score += SIGNAL_WEIGHTS.coreMemoryUpdated;
	if (signals.contradictionDetected) score += SIGNAL_WEIGHTS.contradictionDetected;
	if (signals.userExplicitMarker) score += SIGNAL_WEIGHTS.userExplicitMarker;
	return Math.min(score, 1.0);
}

// ---------------------------------------------------------------------------
// Explicit-marker heuristic
// ---------------------------------------------------------------------------

/**
 * Regex patterns that suggest the owner explicitly asked Theo to remember
 * the current context. Case-insensitive, whole-phrase matches. The list
 * is intentionally conservative -- false positives here can cause the
 * consolidator to hoard low-value episodes, which is worse than the
 * opposite error. Additional patterns should arrive as evidence, not
 * speculation.
 */
const EXPLICIT_MARKERS: readonly RegExp[] = [
	/\bremember (this|that)\b/i,
	/\b(this|that) is important\b/i,
	/\bdon'?t forget\b/i,
	/\bmake a note\b/i,
	/\bkeep (this|that) in mind\b/i,
	/\bsave (this|that) for later\b/i,
];

/** Pure function: detect owner-flagged salience in a message body. */
export function detectExplicitMarker(body: string): boolean {
	return EXPLICIT_MARKERS.some((re) => re.test(body));
}

// ---------------------------------------------------------------------------
// Convenience: kinds that matter for node extraction
// ---------------------------------------------------------------------------

/**
 * Node kinds that count toward the "rich conversation" signal. Abstractions
 * (pattern/principle) and pure episodic lookups (event) don't indicate that
 * the turn itself produced new knowledge, so they are excluded.
 */
export const EXTRACTION_KINDS: ReadonlySet<NodeKind> = new Set<NodeKind>([
	"fact",
	"preference",
	"observation",
	"belief",
	"goal",
	"person",
	"place",
]);
