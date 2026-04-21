/**
 * Unit tests for episode salience scoring.
 *
 * All tests are pure (no DB, no network) so they live with the other
 * unit tests but avoid the helpers.ts Postgres setup.
 */

import { describe, expect, test } from "bun:test";
import {
	BASE_SCORE,
	CONSOLIDATION_GATE,
	detectExplicitMarker,
	EXTRACTION_KINDS,
	KNOWLEDGE_NODES_THRESHOLD,
	type SalienceSignals,
	SIGNAL_WEIGHTS,
	scoreEpisodeImportance,
} from "../../src/memory/salience.ts";

// ---------------------------------------------------------------------------
// scoreEpisodeImportance
// ---------------------------------------------------------------------------

function signals(partial: Partial<SalienceSignals> = {}): SalienceSignals {
	return {
		knowledgeNodesExtracted: 0,
		coreMemoryUpdated: false,
		contradictionDetected: false,
		userExplicitMarker: false,
		...partial,
	};
}

describe("scoreEpisodeImportance", () => {
	test("returns base score when no signals fire", () => {
		expect(scoreEpisodeImportance(signals())).toBe(BASE_SCORE);
	});

	test("rich conversation (>=3 nodes extracted) adds knowledgeNodesExtracted bump", () => {
		const score = scoreEpisodeImportance(
			signals({ knowledgeNodesExtracted: KNOWLEDGE_NODES_THRESHOLD + 2 }),
		);
		expect(score).toBeCloseTo(BASE_SCORE + SIGNAL_WEIGHTS.knowledgeNodesExtracted, 6);
	});

	test("sub-threshold node extraction does not fire the signal", () => {
		const score = scoreEpisodeImportance(
			signals({ knowledgeNodesExtracted: KNOWLEDGE_NODES_THRESHOLD - 1 }),
		);
		expect(score).toBe(BASE_SCORE);
	});

	test("core memory update adds its bump", () => {
		const score = scoreEpisodeImportance(signals({ coreMemoryUpdated: true }));
		expect(score).toBeCloseTo(BASE_SCORE + SIGNAL_WEIGHTS.coreMemoryUpdated, 6);
	});

	test("user explicit marker adds its bump", () => {
		const score = scoreEpisodeImportance(signals({ userExplicitMarker: true }));
		expect(score).toBeCloseTo(BASE_SCORE + SIGNAL_WEIGHTS.userExplicitMarker, 6);
	});

	test("contradiction detected adds its bump", () => {
		const score = scoreEpisodeImportance(signals({ contradictionDetected: true }));
		expect(score).toBeCloseTo(BASE_SCORE + SIGNAL_WEIGHTS.contradictionDetected, 6);
	});

	test("all signals active caps at 1.0", () => {
		const score = scoreEpisodeImportance(
			signals({
				knowledgeNodesExtracted: 10,
				coreMemoryUpdated: true,
				contradictionDetected: true,
				userExplicitMarker: true,
			}),
		);
		expect(score).toBe(1.0);
	});

	test("user marker + core update crosses the consolidation gate", () => {
		const score = scoreEpisodeImportance(
			signals({ coreMemoryUpdated: true, userExplicitMarker: true }),
		);
		expect(score).toBeGreaterThanOrEqual(CONSOLIDATION_GATE);
	});

	test("single knowledge-extraction bump alone does not cross the gate", () => {
		const score = scoreEpisodeImportance(signals({ knowledgeNodesExtracted: 5 }));
		expect(score).toBeLessThan(CONSOLIDATION_GATE);
	});

	test("output is deterministic for identical input", () => {
		const input = signals({ userExplicitMarker: true, knowledgeNodesExtracted: 3 });
		expect(scoreEpisodeImportance(input)).toBe(scoreEpisodeImportance(input));
	});
});

// ---------------------------------------------------------------------------
// detectExplicitMarker
// ---------------------------------------------------------------------------

describe("detectExplicitMarker", () => {
	test("matches 'remember this'", () => {
		expect(detectExplicitMarker("please remember this for later")).toBe(true);
	});

	test("matches 'that is important'", () => {
		expect(detectExplicitMarker("Okay, that is important to keep in mind.")).toBe(true);
	});

	test("matches 'don't forget'", () => {
		expect(detectExplicitMarker("don't forget to follow up next week")).toBe(true);
	});

	test("is case-insensitive", () => {
		expect(detectExplicitMarker("REMEMBER THIS")).toBe(true);
		expect(detectExplicitMarker("Remember That")).toBe(true);
	});

	test("returns false for ordinary conversation", () => {
		expect(detectExplicitMarker("just chatting about the weather")).toBe(false);
	});

	test("returns false for near-misses", () => {
		// The verb "remembered" (past tense) should NOT match the
		// "remember this" pattern -- we want a present-tense command.
		expect(detectExplicitMarker("I remembered the milk yesterday")).toBe(false);
	});

	test("returns false for empty body", () => {
		expect(detectExplicitMarker("")).toBe(false);
	});
});

// ---------------------------------------------------------------------------
// EXTRACTION_KINDS
// ---------------------------------------------------------------------------

describe("EXTRACTION_KINDS", () => {
	test("includes the evidence-producing node kinds", () => {
		expect(EXTRACTION_KINDS.has("fact")).toBe(true);
		expect(EXTRACTION_KINDS.has("preference")).toBe(true);
		expect(EXTRACTION_KINDS.has("observation")).toBe(true);
		expect(EXTRACTION_KINDS.has("belief")).toBe(true);
		expect(EXTRACTION_KINDS.has("goal")).toBe(true);
		expect(EXTRACTION_KINDS.has("person")).toBe(true);
		expect(EXTRACTION_KINDS.has("place")).toBe(true);
	});

	test("excludes abstraction and episodic-only kinds", () => {
		expect(EXTRACTION_KINDS.has("pattern")).toBe(false);
		expect(EXTRACTION_KINDS.has("principle")).toBe(false);
		expect(EXTRACTION_KINDS.has("event")).toBe(false);
	});
});
