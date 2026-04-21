/**
 * Consolidation.
 *
 * Scheduled job (Phase 12) that compacts old state and distills higher-level
 * abstractions. This module holds the orchestration logic; the individual
 * pieces (forgetting curves, abstraction synthesis) live in their own files.
 *
 * The consolidation pass has these stages, in order:
 *
 *   1. Compress episodes older than 7 days into summary episodes. Uses the
 *      `episode.summarize_requested` / `episode.summarized` decision/effect
 *      pair — the summarizer call is an effect handler, so replay does not
 *      re-summarize existing history.
 *   2. Deduplicate near-identical nodes (cosine similarity > 0.95). Uses
 *      `mergeNodes()` which runs a full SQL transaction redirecting edges
 *      and episode_node references, then emits `memory.node.merged`.
 *   3. Apply forgetting curves (decay importance, skip patterns/principles).
 *   4. Normalize importance (guard against unbounded propagation drift).
 *   5. Synthesize abstractions (clusters → pattern nodes; patterns →
 *      principle nodes).
 *
 * Returns a structured `ConsolidationResult` summary. Errors in one stage do
 * not abort the others — each stage catches its own failures and logs them.
 */

import type { Sql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import { describeError } from "../errors.ts";
import type { EventBus } from "../events/bus.ts";
import type { EventOfType } from "../events/types.ts";
import type { AbstractionDeps } from "./abstraction.ts";
import { synthesizeAbstractions } from "./abstraction.ts";
import type { EpisodicRepository } from "./episodic.ts";
import { applyForgettingCurves, type ForgettingDeps } from "./forgetting.ts";
import type { NodeId } from "./graph/types.ts";
import { asNodeId } from "./graph/types.ts";
import { cheapQuery } from "./llm.ts";
import { normalizeImportance, type PropagationDeps } from "./propagation.ts";

// ---------------------------------------------------------------------------
// Tunables
// ---------------------------------------------------------------------------

/** Episodes older than this are candidates for compression. */
const EPISODE_AGE_DAYS = 7;

/** Cosine similarity above which two same-kind nodes are deduplicated. */
const DEDUP_SIMILARITY_THRESHOLD = 0.95;

/** Max node merges per run. */
const MAX_MERGES_PER_RUN = 50;

// ---------------------------------------------------------------------------
// Summarizer seam
// ---------------------------------------------------------------------------

export type Summarizer = (transcript: string) => Promise<string>;

/**
 * Default summarizer uses `query()` with `model: "haiku"` and no tools. The
 * summary is returned as plain text (no JSON schema — the response is free-
 * form prose).
 */
export async function defaultSummarizer(transcript: string): Promise<string> {
	const { text } = await cheapQuery({
		prompt:
			"Summarize this conversation into a concise paragraph that preserves " +
			`key facts, decisions, and action items:\n\n${transcript}`,
	});
	return text ?? "Summary generation failed.";
}

// ---------------------------------------------------------------------------
// Dependencies + result shape
// ---------------------------------------------------------------------------

export interface ConsolidationDeps {
	readonly sql: Sql;
	readonly bus: EventBus;
	readonly episodic: EpisodicRepository;
	readonly abstraction: AbstractionDeps;
	readonly forgetting: ForgettingDeps;
	readonly propagation: PropagationDeps;
	readonly summarizer?: Summarizer;
	readonly now?: () => Date;
}

export interface ConsolidationResult {
	readonly episodesCompressed: number;
	readonly nodesMerged: number;
	readonly nodesDecayed: number;
	readonly importanceRescaled: boolean;
	readonly abstractionsSynthesized: number;
	readonly errors: readonly string[];
}

// ---------------------------------------------------------------------------
// Top-level orchestrator
// ---------------------------------------------------------------------------

/**
 * Run the full consolidation pipeline. Each stage is independent — errors
 * are captured and surfaced in `result.errors` but do not abort the run.
 */
export async function consolidate(deps: ConsolidationDeps): Promise<ConsolidationResult> {
	const errors: string[] = [];
	const summarizer = deps.summarizer ?? defaultSummarizer;

	let episodesCompressed = 0;
	try {
		episodesCompressed = await compressOldEpisodes(deps, summarizer);
	} catch (error: unknown) {
		errors.push(`compressOldEpisodes: ${describeError(error)}`);
	}

	let nodesMerged = 0;
	try {
		nodesMerged = await deduplicateNodes(deps);
	} catch (error: unknown) {
		errors.push(`deduplicateNodes: ${describeError(error)}`);
	}

	let nodesDecayed = 0;
	try {
		nodesDecayed = await applyForgettingCurves(deps.forgetting);
	} catch (error: unknown) {
		errors.push(`applyForgettingCurves: ${describeError(error)}`);
	}

	let importanceRescaled = false;
	try {
		const mean = await normalizeImportance(deps.propagation);
		importanceRescaled = mean !== null;
	} catch (error: unknown) {
		errors.push(`normalizeImportance: ${describeError(error)}`);
	}

	let abstractionsSynthesized = 0;
	try {
		abstractionsSynthesized = await synthesizeAbstractions(deps.abstraction);
	} catch (error: unknown) {
		errors.push(`synthesizeAbstractions: ${describeError(error)}`);
	}

	return {
		episodesCompressed,
		nodesMerged,
		nodesDecayed,
		importanceRescaled,
		abstractionsSynthesized,
		errors,
	};
}

// ---------------------------------------------------------------------------
// Stage 1: episode compression via decision/effect split
// ---------------------------------------------------------------------------

interface EpisodeRecord {
	readonly id: number;
	readonly sessionId: string;
	readonly role: "user" | "assistant";
	readonly body: string;
	readonly createdAt: Date;
}

/**
 * Compress old episodes. Per session, emit `episode.summarize_requested`
 * (decision), call the summarizer as an effect handler surrogate, then emit
 * `episode.summarized` (effect). The summarize-applier decision handler —
 * wired by `registerEpisodeSummarizedApplier` — creates the summary episode
 * and marks originals as superseded.
 *
 * During this live call we run both stages inline for simplicity. The replay
 * determinism property is preserved because the effect stage is not replayed
 * (bus `mode: "effect"` fast-forwards), and the applier (decision) replays
 * from the captured `episode.summarized` event.
 */
export async function compressOldEpisodes(
	deps: ConsolidationDeps,
	summarizer: Summarizer,
): Promise<number> {
	const now = deps.now?.() ?? new Date();
	const ageCutoff = new Date(now.getTime() - EPISODE_AGE_DAYS * 86400 * 1000);

	const rawRows = await deps.sql`
		SELECT id, session_id, role, body, created_at
		FROM episode
		WHERE created_at < ${ageCutoff}
		  AND superseded_by IS NULL
		ORDER BY session_id, created_at ASC
	`;
	if (rawRows.length === 0) return 0;

	const rows: EpisodeRecord[] = rawRows.map((row) => ({
		id: row["id"] as number,
		sessionId: row["session_id"] as string,
		role: row["role"] as "user" | "assistant",
		body: row["body"] as string,
		createdAt: row["created_at"] as Date,
	}));

	const bySession = new Map<string, EpisodeRecord[]>();
	for (const row of rows) {
		const existing = bySession.get(row.sessionId);
		if (existing) existing.push(row);
		else bySession.set(row.sessionId, [row]);
	}

	let compressed = 0;
	for (const [sessionId, episodes] of bySession) {
		const episodeIds = episodes.map((e) => e.id);

		await deps.bus.emit({
			type: "episode.summarize_requested",
			version: 1,
			actor: "system",
			data: { sessionId, episodeIds },
			metadata: { sessionId },
		});

		const transcript = episodes.map((e) => `[${e.role}]: ${e.body}`).join("\n");
		let summary: string;
		try {
			summary = await summarizer(transcript);
		} catch (error: unknown) {
			console.warn(`Summarizer failed for session ${sessionId}: ${describeError(error)}`);
			continue;
		}
		if (summary.length === 0) continue;

		await deps.bus.emit({
			type: "episode.summarized",
			version: 1,
			actor: "system",
			data: { sessionId, episodeIds, summary },
			metadata: { sessionId },
		});

		compressed += episodeIds.length;
	}

	return compressed;
}

/**
 * Decision handler for `episode.summarized` — takes the captured summary and
 * writes the consolidated episode + `superseded_by` pointers. Because the
 * summary text is in the event payload, replay rebuilds identical state
 * without calling the summarizer.
 */
export function registerEpisodeSummarizedApplier(deps: {
	readonly bus: EventBus;
	readonly sql: Sql;
	readonly episodic: EpisodicRepository;
}): void {
	deps.bus.on(
		"episode.summarized",
		async (event) => {
			await applyEpisodeSummary(event, deps);
		},
		{ id: "episode-summary-applier", mode: "decision" },
	);
}

async function applyEpisodeSummary(
	event: EventOfType<"episode.summarized">,
	deps: { readonly sql: Sql; readonly episodic: EpisodicRepository },
): Promise<void> {
	const { sessionId, episodeIds, summary } = event.data;
	if (summary.length === 0 || episodeIds.length === 0) return;

	// The `episodic.append()` call emits `memory.episode.created`. Afterwards
	// we update `superseded_by` on the originals in a separate statement —
	// the append must come first to get the new ID.
	const consolidated = await deps.episodic.append({
		sessionId,
		role: "assistant",
		body: summary,
		actor: "system",
	});

	const ids = episodeIds.map(Number);
	await deps.sql`
		UPDATE episode
		SET superseded_by = ${consolidated.id}
		WHERE id = ANY(${ids}::int[])
		  AND id <> ${consolidated.id}
		  AND superseded_by IS NULL
	`;
}

// ---------------------------------------------------------------------------
// Stage 2: node deduplication
// ---------------------------------------------------------------------------

interface DuplicatePair {
	readonly idA: number;
	readonly idB: number;
	readonly similarity: number;
}

async function deduplicateNodes(deps: ConsolidationDeps): Promise<number> {
	const rawDups = await deps.sql`
		SELECT a.id AS id_a, b.id AS id_b,
		       (1 - (a.embedding <=> b.embedding))::real AS similarity
		FROM node a
		JOIN node b ON a.id < b.id AND a.kind = b.kind
		WHERE a.embedding IS NOT NULL AND b.embedding IS NOT NULL
		  AND (1 - (a.embedding <=> b.embedding)) > ${DEDUP_SIMILARITY_THRESHOLD}
		  AND a.kind NOT IN ('pattern', 'principle')
		  AND a.confidence > 0
		  AND b.confidence > 0
		ORDER BY similarity DESC
		LIMIT ${MAX_MERGES_PER_RUN}
	`;

	if (rawDups.length === 0) return 0;

	const duplicates: DuplicatePair[] = rawDups.map((row) => ({
		idA: row["id_a"] as number,
		idB: row["id_b"] as number,
		similarity: row["similarity"] as number,
	}));

	// Track which IDs have already been merged (soft-deleted). Subsequent
	// pairs referencing a merged ID are skipped — the mergedId has confidence
	// = 0 and should not cascade into further merges.
	const retired = new Set<number>();
	let merged = 0;
	for (const dup of duplicates) {
		if (retired.has(dup.idA) || retired.has(dup.idB)) continue;
		const { retiredId } = await mergeNodes(asNodeId(dup.idA), asNodeId(dup.idB), deps);
		retired.add(retiredId);
		merged++;
	}
	return merged;
}

/**
 * Merge two near-duplicate nodes. The node with the higher
 * `(confidence, importance)` tuple wins — edges and episode references are
 * redirected to the winner, the loser is soft-deleted (confidence = 0) and
 * a `merged_into` edge connects the loser to the winner for traceability.
 *
 * Emits `memory.node.merged` inside the same transaction as the mutations.
 */
export async function mergeNodes(
	idA: NodeId,
	idB: NodeId,
	deps: { readonly sql: Sql; readonly bus: EventBus },
): Promise<{ readonly keptId: NodeId; readonly retiredId: NodeId }> {
	return deps.sql.begin(async (tx) => {
		const q = asQueryable(tx);

		// Lock both rows and inspect their confidence/importance to decide the winner.
		const rows = await q`
			SELECT id, confidence, importance
			FROM node
			WHERE id IN (${idA}, ${idB})
			ORDER BY id
			FOR UPDATE
		`;
		if (rows.length !== 2) {
			throw new Error(`mergeNodes: expected 2 rows for ${String(idA)}/${String(idB)}`);
		}
		const a = rows.find((r) => (r["id"] as number) === idA);
		const b = rows.find((r) => (r["id"] as number) === idB);
		if (a === undefined || b === undefined) {
			throw new Error(`mergeNodes: mismatched rows for ${String(idA)}/${String(idB)}`);
		}

		// Tiebreak: higher confidence wins; if equal, higher importance; if still
		// equal, keep the smaller id (stable).
		const aConf = a["confidence"] as number;
		const bConf = b["confidence"] as number;
		const aImp = a["importance"] as number;
		const bImp = b["importance"] as number;
		const keepA = aConf > bConf || (aConf === bConf && aImp >= bImp);
		const keptId = keepA ? idA : idB;
		const retiredId = keepA ? idB : idA;

		// 1. Redirect active edges pointing to the retired node.
		await q`
			UPDATE edge SET source_id = ${keptId}
			WHERE source_id = ${retiredId} AND valid_to IS NULL
			  AND NOT EXISTS (
			    SELECT 1 FROM edge e2
			    WHERE e2.source_id = ${keptId}
			      AND e2.target_id = edge.target_id
			      AND e2.label = edge.label
			      AND e2.valid_to IS NULL
			      AND e2.id <> edge.id
			  )
		`;
		await q`
			UPDATE edge SET target_id = ${keptId}
			WHERE target_id = ${retiredId} AND valid_to IS NULL
			  AND NOT EXISTS (
			    SELECT 1 FROM edge e2
			    WHERE e2.target_id = ${keptId}
			      AND e2.source_id = edge.source_id
			      AND e2.label = edge.label
			      AND e2.valid_to IS NULL
			      AND e2.id <> edge.id
			  )
		`;
		// Any remaining edges (the NOT EXISTS filter ruled them out to avoid
		// violating the unique active-edge index) are expired — their content
		// was duplicative.
		await q`
			UPDATE edge SET valid_to = now()
			WHERE (source_id = ${retiredId} OR target_id = ${retiredId})
			  AND valid_to IS NULL
		`;

		// 2. Redirect episode_node cross-references.
		await q`
			INSERT INTO episode_node (episode_id, node_id)
			SELECT episode_id, ${keptId}
			FROM episode_node
			WHERE node_id = ${retiredId}
			ON CONFLICT (episode_id, node_id) DO NOTHING
		`;
		await q`DELETE FROM episode_node WHERE node_id = ${retiredId}`;

		// 3. Take the higher of (confidence, importance).
		await q`
			UPDATE node SET
				confidence = GREATEST(${aConf}::real, ${bConf}::real),
				importance = GREATEST(${aImp}::real, ${bImp}::real)
			WHERE id = ${keptId}
		`;

		// 4. Soft-delete the loser: confidence = 0 hides it from retrieval,
		//    but the row survives so event replay can still resolve old IDs.
		await q`UPDATE node SET confidence = 0 WHERE id = ${retiredId}`;

		// 5. Trace edge keptId -> retiredId so readers can follow merges.
		await q`
			INSERT INTO edge (source_id, target_id, label, weight)
			VALUES (${retiredId}, ${keptId}, 'merged_into', 1.0)
			ON CONFLICT DO NOTHING
		`;

		// 6. Emit memory.node.merged within the same transaction.
		await deps.bus.emit(
			{
				type: "memory.node.merged",
				version: 1,
				actor: "system",
				data: { keptId, mergedId: retiredId },
				metadata: {},
			},
			{ tx },
		);

		return { keptId, retiredId };
	});
}
