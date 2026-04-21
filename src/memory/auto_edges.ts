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
import type { EventBus } from "../events/bus.ts";
import type { EventOfType } from "../events/types.ts";
import type { EdgeRepository } from "./graph/edges.ts";
import { asNodeId } from "./graph/types.ts";

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

interface NewPair {
	readonly sourceId: number;
	readonly targetId: number;
	readonly delta: number;
}

/**
 * Scan one session's episodes for co-occurring node pairs, strengthen
 * existing `co_occurs` edges in one batched UPDATE, then create edges for
 * any pairs that didn't previously exist.
 */
export async function discoverAutoEdges(
	event: EventOfType<"turn.completed">,
	deps: AutoEdgeDeps,
): Promise<void> {
	const sessionId = event.data.sessionId;
	if (sessionId.length === 0) return;

	// Strengthen existing active co_occurs edges in one pass. The CTE computes
	// the per-pair delta from episode_node co-occurrence; the UPDATE saturates
	// at MAX_CO_OCCURS_WEIGHT and skips no-op rows (weight already at cap).
	const strengthened = await deps.sql<{ readonly s: number; readonly t: number }[]>`
		WITH pairs AS (
			SELECT a.node_id AS source_id,
			       b.node_id AS target_id,
			       COUNT(DISTINCT a.episode_id)::real * ${WEIGHT_PER_CO_OCCURRENCE}::real AS delta
			FROM episode_node a
			JOIN episode_node b ON a.episode_id = b.episode_id AND a.node_id < b.node_id
			JOIN episode e ON e.id = a.episode_id
			WHERE e.session_id = ${sessionId}
			GROUP BY a.node_id, b.node_id
		)
		UPDATE edge SET weight = LEAST(${MAX_CO_OCCURS_WEIGHT}::real, edge.weight + pairs.delta)
		FROM pairs
		WHERE edge.source_id = pairs.source_id
		  AND edge.target_id = pairs.target_id
		  AND edge.label = 'co_occurs'
		  AND edge.valid_to IS NULL
		  AND edge.weight < ${MAX_CO_OCCURS_WEIGHT}::real
		RETURNING edge.source_id AS s, edge.target_id AS t
	`;
	const seen = new Set<string>(strengthened.map((r) => `${r.s}:${r.t}`));

	// New pairs — create via EdgeRepository so events fire correctly.
	const newPairs = await deps.sql<NewPair[]>`
		SELECT a.node_id AS "sourceId",
		       b.node_id AS "targetId",
		       (COUNT(DISTINCT a.episode_id)::real * ${WEIGHT_PER_CO_OCCURRENCE}::real) AS delta
		FROM episode_node a
		JOIN episode_node b ON a.episode_id = b.episode_id AND a.node_id < b.node_id
		JOIN episode e ON e.id = a.episode_id
		WHERE e.session_id = ${sessionId}
		  AND NOT EXISTS (
		    SELECT 1 FROM edge x
		    WHERE x.source_id = a.node_id
		      AND x.target_id = b.node_id
		      AND x.label = 'co_occurs'
		      AND x.valid_to IS NULL
		  )
		GROUP BY a.node_id, b.node_id
	`;

	for (const pair of newPairs) {
		if (seen.has(`${pair.sourceId}:${pair.targetId}`)) continue;
		await deps.edges.create({
			sourceId: asNodeId(pair.sourceId),
			targetId: asNodeId(pair.targetId),
			label: "co_occurs",
			weight: Math.min(MAX_CO_OCCURS_WEIGHT, pair.delta),
			actor: "system",
		});
	}
}
