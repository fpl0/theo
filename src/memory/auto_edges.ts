/**
 * Auto-edge discovery.
 *
 * On every `turn.completed`, count node pairs that co-occurred in the same
 * session's episodes and strengthen or create `co_occurs` edges. Fully
 * deterministic over the graph state (no LLM calls, no network) → decision
 * handler that runs on replay as well as live dispatch.
 *
 * Weight policy:
 * - Each co-occurrence contributes +0.5 per tick.
 * - Saturates at 5.0 (matches the `edge.weight` CHECK constraint).
 */

import type { Sql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { EventOfType } from "../events/types.ts";
import type { EdgeRepository } from "./graph/edges.ts";
import { asEdgeId, asNodeId } from "./graph/types.ts";

/** Weight added per co-occurrence within a single tick. */
const WEIGHT_PER_CO_OCCURRENCE = 0.5;

/** Hard cap — matches the `edge.weight` CHECK constraint. */
export const MAX_CO_OCCURS_WEIGHT = 5.0;

export interface AutoEdgeDeps {
	readonly sql: Sql;
	readonly bus: EventBus;
	readonly edges: EdgeRepository;
}

/** Register the turn.completed handler. Decision handler — replay-safe. */
export function registerAutoEdgeHandler(deps: AutoEdgeDeps): void {
	deps.bus.on(
		"turn.completed",
		async (event) => {
			await discoverAutoEdges(event, deps);
		},
		{ id: "auto-edge-discovery", mode: "decision" },
	);
}

interface CoOccurrencePair {
	readonly sourceId: number;
	readonly targetId: number;
	readonly coCount: number;
}

/**
 * Scan one session's episodes for co-occurring node pairs and update or
 * create the corresponding `co_occurs` edges.
 */
export async function discoverAutoEdges(
	event: EventOfType<"turn.completed">,
	deps: AutoEdgeDeps,
): Promise<void> {
	const sessionId = event.data.sessionId;
	if (sessionId.length === 0) return;

	// Self-join on episode_node restricted to one session. The `a.node_id < b.node_id`
	// predicate both de-duplicates pairs and excludes self-edges. Counts distinct
	// episodes, not distinct rows — so each episode contributes at most one +0.5.
	const rawPairs = await deps.sql`
		SELECT a.node_id AS source_id,
		       b.node_id AS target_id,
		       COUNT(DISTINCT a.episode_id) AS co_count
		FROM episode_node a
		JOIN episode_node b ON a.episode_id = b.episode_id AND a.node_id < b.node_id
		JOIN episode e ON e.id = a.episode_id
		WHERE e.session_id = ${sessionId}
		GROUP BY a.node_id, b.node_id
	`;

	const pairs: CoOccurrencePair[] = rawPairs.map((row) => ({
		sourceId: row["source_id"] as number,
		targetId: row["target_id"] as number,
		coCount: Number(row["co_count"]),
	}));

	for (const pair of pairs) {
		await upsertCoOccursEdge(pair, deps);
	}
}

async function upsertCoOccursEdge(pair: CoOccurrencePair, deps: AutoEdgeDeps): Promise<void> {
	const existingRows = await deps.sql`
		SELECT id, weight FROM edge
		WHERE source_id = ${pair.sourceId}
		  AND target_id = ${pair.targetId}
		  AND label = 'co_occurs'
		  AND valid_to IS NULL
	`;
	const existing = existingRows[0];
	const delta = pair.coCount * WEIGHT_PER_CO_OCCURRENCE;

	if (existing !== undefined) {
		const currentWeight = existing["weight"] as number;
		const newWeight = Math.min(MAX_CO_OCCURS_WEIGHT, currentWeight + delta);
		if (newWeight <= currentWeight) return;
		// Update in place without temporal versioning — auto-edge strengthening
		// is a running aggregate, not a semantic change. Keeping the same edge
		// row avoids churn in the edge table for the most common write.
		const edgeId = asEdgeId(existing["id"] as number);
		await deps.sql.begin(async (tx) => {
			const q = asQueryable(tx);
			await q`UPDATE edge SET weight = ${newWeight} WHERE id = ${edgeId}`;
		});
		return;
	}

	const weight = Math.min(MAX_CO_OCCURS_WEIGHT, delta);
	await deps.edges.create({
		sourceId: asNodeId(pair.sourceId),
		targetId: asNodeId(pair.targetId),
		label: "co_occurs",
		weight,
		actor: "system",
	});
}
