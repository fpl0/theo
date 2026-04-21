/**
 * Importance propagation.
 *
 * When nodes are retrieved by RRF search, their graph neighbors get a small
 * importance boost. Inspired by spreading activation in cognitive science —
 * frequently-co-activated concepts stay salient together.
 *
 * Deltas are deliberately tiny:
 *   - 1-hop neighbor: +0.02 * edge_weight
 *   - 2-hop neighbor: +0.01 * edge_weight
 *
 * Propagation alone cannot push a node to high importance; it needs repeated
 * activation. The consolidation job periodically normalizes the mean
 * importance (see `normalizeImportance`) to prevent unbounded drift.
 *
 * The handler fires on `memory.node.accessed` (emitted by `RetrievalService`
 * on every successful search), which already carries the full list of
 * accessed node IDs — the handler re-reads the active graph and applies
 * a single batched UPDATE per hop depth.
 */

import type { Sql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { EventOfType } from "../events/types.ts";

/** Max importance any node can reach. */
const IMPORTANCE_CEILING = 1.0;

/** Delta per 1-hop neighbor, per unit of edge weight. */
export const HOP_1_DELTA = 0.02;

/** Delta per 2-hop neighbor, per unit of edge weight. */
export const HOP_2_DELTA = 0.01;

/** Mean-importance threshold above which `normalizeImportance` rescales. */
export const NORMALIZATION_THRESHOLD = 0.6;

/** Target mean after normalization. */
export const NORMALIZATION_TARGET = 0.5;

export interface PropagationDeps {
	readonly sql: Sql;
	readonly bus: EventBus;
}

/**
 * Register the retrieval-driven propagation handler. Decision handler —
 * deterministic over the edge graph and the accessed-node list, so replay
 * reconstructs the same importance boosts.
 */
export function registerPropagationHandler(deps: PropagationDeps): void {
	deps.bus.on(
		"memory.node.accessed",
		async (event) => {
			await propagateImportance(event, deps);
		},
		{ id: "importance-propagator", mode: "decision" },
	);
}

/**
 * Propagate importance from a set of accessed nodes to their 1- and 2-hop
 * neighbors. The source nodes themselves are excluded from boosts to avoid
 * self-reinforcement on retrieval.
 */
export async function propagateImportance(
	event: EventOfType<"memory.node.accessed">,
	deps: PropagationDeps,
): Promise<void> {
	const seedIds = event.data.nodeIds.map(Number);
	if (seedIds.length === 0) return;

	// Single SQL pass: compute boost per distinct neighbor using a recursive
	// CTE capped at depth 2. MAX(boost) deduplicates when a neighbor is
	// reachable through multiple paths at different depths.
	const { hop1Count, hop2Count } = await deps.sql.begin(async (tx) => {
		const q = asQueryable(tx);

		const rows = await q<{ readonly id: number; readonly boost: number; readonly depth: number }[]>`
			WITH RECURSIVE walk AS (
				SELECT id, 0::int AS depth, 0::real AS contribution
				FROM node WHERE id = ANY(${seedIds}::int[])

				UNION ALL

				SELECT
					CASE WHEN e.source_id = w.id THEN e.target_id ELSE e.source_id END AS id,
					w.depth + 1 AS depth,
					e.weight::real AS contribution
				FROM walk w
				JOIN edge e ON (e.source_id = w.id OR e.target_id = w.id)
					AND e.valid_to IS NULL
				WHERE w.depth < 2
			),
			boosts AS (
				SELECT id,
				       depth,
				       MAX(contribution) AS contribution
				FROM walk
				WHERE depth > 0
				  AND id <> ALL(${seedIds}::int[])
				GROUP BY id, depth
			),
			ranked AS (
				SELECT id,
				       MIN(depth) AS depth,
				       MAX(CASE WHEN depth = 1 THEN ${HOP_1_DELTA}::real
				                ELSE ${HOP_2_DELTA}::real END * contribution) AS boost
				FROM boosts
				GROUP BY id
			)
			UPDATE node n
			SET importance = LEAST(${IMPORTANCE_CEILING}::real, n.importance + r.boost)
			FROM ranked r
			WHERE n.id = r.id
			  AND r.boost > 0
			RETURNING n.id, r.boost AS boost, r.depth AS depth
		`;

		let hop1 = 0;
		let hop2 = 0;
		for (const row of rows) {
			if (row.depth === 1) hop1++;
			else if (row.depth === 2) hop2++;
		}
		return { hop1Count: hop1, hop2Count: hop2 };
	});

	if (hop1Count === 0 && hop2Count === 0) return;

	await deps.bus.emit({
		type: "memory.node.importance.propagated",
		version: 1,
		actor: "system",
		data: {
			// Encode summary in the aggregated event: nodeId records the primary
			// seed (first accessed node), boostDelta is the largest delta
			// applied (HOP_1_DELTA is the cap for 1-hop), hopsTraversed covers
			// both depths.
			nodeId: seedIds[0] ?? 0,
			boostDelta: hop1Count > 0 ? HOP_1_DELTA : HOP_2_DELTA,
			hopsTraversed: hop2Count > 0 ? 2 : 1,
		},
		metadata: { causeId: event.id },
	});
}

/**
 * Rescale all importances proportionally when the mean drifts above
 * `NORMALIZATION_THRESHOLD`. Pattern and principle nodes participate in the
 * rescale — their *relative* importance is preserved.
 *
 * Returns the old mean (or null if no rescale was needed).
 */
export async function normalizeImportance(deps: PropagationDeps): Promise<number | null> {
	return deps.sql.begin(async (tx) => {
		const q = asQueryable(tx);
		const rows = await q`SELECT AVG(importance)::real AS mean FROM node`;
		const first = rows[0];
		if (first === undefined) return null;
		const mean = first["mean"] as number | null;
		if (mean === null || mean <= NORMALIZATION_THRESHOLD) return null;

		const scale = NORMALIZATION_TARGET / mean;
		await q`
			UPDATE node
			SET importance = GREATEST(
				${0.0}::real,
				LEAST(${IMPORTANCE_CEILING}::real, importance * ${scale}::real)
			)
		`;
		return mean;
	});
}
