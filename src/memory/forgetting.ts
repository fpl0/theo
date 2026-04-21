/**
 * Forgetting curves.
 *
 * Exponential decay on node importance, modified by access frequency. Runs as
 * part of the consolidation job. Pure SQL — deterministic over clock time and
 * the current graph state. Emits a single `memory.node.decayed` event per run
 * summarizing the batch.
 *
 * Key parameters (see `docs/plans/foundation/13-background-intelligence.md`):
 *   - Base half-life: 30 days.
 *   - Access-frequency multiplier: `1 + access_count * 0.1`. A node accessed 10
 *     times has 2× the effective half-life.
 *   - Floor: 0.05 — nodes never fully disappear. Decay affects ranking, not
 *     existence.
 *   - Pattern / principle nodes are immune. They represent distilled knowledge
 *     synthesized from clusters and must not be eroded by disuse.
 */

import type { Sql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";

/** Minimum importance any node can decay to. */
export const IMPORTANCE_FLOOR = 0.05;

/** Half-life in days for a never-accessed node. */
export const BASE_HALF_LIFE_DAYS = 30;

/** Per-access extension factor. */
const ACCESS_HALF_LIFE_MULTIPLIER = 0.1;

export interface ForgettingDeps {
	readonly sql: Sql;
	readonly bus: EventBus;
	/** Injectable clock for tests. Defaults to `() => new Date()`. */
	readonly now?: () => Date;
}

/**
 * Compute the decayed importance for a single node.
 *
 * `days` is the time since the last access (or creation, if never accessed).
 * The effective half-life is `BASE_HALF_LIFE_DAYS * (1 + accessCount * 0.1)`,
 * so high-access nodes decay more slowly. Below the floor, the value is
 * clamped; above 1.0, clamped to 1.0.
 */
export function computeDecayedImportance(
	currentImportance: number,
	accessCount: number,
	days: number,
): number {
	if (days <= 0) return clamp(currentImportance);
	const halfLife = BASE_HALF_LIFE_DAYS * (1 + accessCount * ACCESS_HALF_LIFE_MULTIPLIER);
	const factor = 0.5 ** (days / halfLife);
	return clamp(currentImportance * factor);
}

function clamp(v: number): number {
	if (v < IMPORTANCE_FLOOR) return IMPORTANCE_FLOOR;
	if (v > 1) return 1;
	return v;
}

/**
 * Apply forgetting curves to every eligible node in a single SQL pass. Emits
 * `memory.node.decayed` with a count + post-floor summary and returns the
 * number of rows affected.
 *
 * Pattern and principle nodes are excluded from the UPDATE. Rows already at
 * the floor are left alone — the UPDATE is bounded to nodes whose decay
 * would actually lower importance.
 */
export async function applyForgettingCurves(deps: ForgettingDeps): Promise<number> {
	const now = deps.now?.() ?? new Date();

	const result = await deps.sql.begin(async (tx) => {
		const q = asQueryable(tx);

		// SQL computes the same formula as computeDecayedImportance() — keep
		// the two in lockstep when tuning. The COALESCE lets brand-new nodes
		// with no last_accessed_at be measured from created_at.
		const rows = await q`
			WITH decayed AS (
				SELECT id,
				       importance AS old_importance,
				       GREATEST(
				         ${IMPORTANCE_FLOOR}::real,
				         importance * power(
				           0.5,
				           EXTRACT(EPOCH FROM (${now}::timestamptz - COALESCE(last_accessed_at, created_at)))
				             / (86400.0 * ${BASE_HALF_LIFE_DAYS}::real
				                  * (1 + access_count * ${ACCESS_HALF_LIFE_MULTIPLIER}::real))
				         )
				       )::real AS new_importance
				FROM node
				WHERE kind NOT IN ('pattern', 'principle')
				  AND importance > ${IMPORTANCE_FLOOR}::real
			)
			UPDATE node n
			SET importance = d.new_importance
			FROM decayed d
			WHERE n.id = d.id
			  AND d.new_importance < n.importance
			RETURNING n.id, n.importance
		`;

		return rows;
	});

	if (result.length === 0) return 0;

	const minImportanceAfter = Math.min(...result.map((r) => r["importance"] as number));
	await deps.bus.emit({
		type: "memory.node.decayed",
		version: 1,
		actor: "system",
		data: { nodeCount: result.length, minImportanceAfter },
		metadata: {},
	});

	return result.length;
}
