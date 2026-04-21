/**
 * Forgetting curves.
 *
 * Exponential decay on node importance, modified by access frequency and
 * node kind. Runs as part of the consolidation job. Pure SQL —
 * deterministic over clock time and the current graph state. Emits a
 * single `memory.node.decayed` event per run summarizing the batch.
 *
 * Key parameters (see `docs/plans/foundation/13a-memory-resilience.md`):
 *   - Per-kind half-life: defined in HALF_LIFE_DAYS. Stable kinds
 *     (preference, belief) decay slowly; situational kinds (observation,
 *     event) decay fast.
 *   - Access-frequency multiplier: `1 + access_count * 0.1`. A node
 *     accessed 10 times has 2x the effective half-life.
 *   - Floor: 0.05 — nodes never fully disappear. Decay affects ranking,
 *     not existence.
 *   - Pattern / principle nodes are immune. They represent distilled
 *     knowledge and must not be eroded by disuse. Expressed here by
 *     sentinel `Infinity` half-lives that the SQL pass translates into
 *     a hard skip.
 */

import type { Sql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { NodeKind } from "../events/types.ts";

/** Minimum importance any node can decay to. */
export const IMPORTANCE_FLOOR = 0.05;

/**
 * Base half-life for a generic node kind with no access history. Kept for
 * backward-compat with callers that treated the old 30-day value as
 * canonical (e.g., unit tests for computeDecayedImportance).
 */
export const BASE_HALF_LIFE_DAYS = 30;

/** Per-access extension factor. */
const ACCESS_HALF_LIFE_MULTIPLIER = 0.1;

/**
 * Per-kind half-lives (days). `Infinity` means the kind never decays.
 * The values reflect how quickly knowledge of each kind tends to go
 * stale in practice:
 *
 *   - preference (120d): stable personal preferences
 *   - belief (90d), person (90d), place (90d): slow-moving
 *   - goal (60d): goals shift but not weekly
 *   - fact (30d): default, facts can become stale
 *   - observation (14d), event (14d): situational, decay fast
 *   - pattern / principle: exempt, distilled knowledge
 */
export const HALF_LIFE_DAYS: Readonly<Record<NodeKind, number>> = {
	preference: 120,
	belief: 90,
	person: 90,
	place: 90,
	goal: 60,
	fact: 30,
	observation: 14,
	event: 14,
	pattern: Number.POSITIVE_INFINITY,
	principle: Number.POSITIVE_INFINITY,
};

export interface ForgettingDeps {
	readonly sql: Sql;
	readonly bus: EventBus;
	/** Injectable clock for tests. Defaults to `() => new Date()`. */
	readonly now?: () => Date;
}

/**
 * Compute the decayed importance for a single node with a kind-aware
 * half-life. `days` is time since last access (or creation).
 *
 * - If the kind has half-life Infinity, the value is unchanged (clamped).
 * - If `days <= 0`, the value is unchanged (clamped).
 * - Otherwise the effective half-life is
 *   `HALF_LIFE_DAYS[kind] * (1 + accessCount * 0.1)`.
 */
export function computeDecayedImportance(
	currentImportance: number,
	accessCount: number,
	days: number,
	kind: NodeKind = "fact",
): number {
	if (!Number.isFinite(HALF_LIFE_DAYS[kind])) return clamp(currentImportance);
	if (days <= 0) return clamp(currentImportance);
	const halfLife = HALF_LIFE_DAYS[kind] * (1 + accessCount * ACCESS_HALF_LIFE_MULTIPLIER);
	const factor = 0.5 ** (days / halfLife);
	return clamp(currentImportance * factor);
}

function clamp(v: number): number {
	if (v < IMPORTANCE_FLOOR) return IMPORTANCE_FLOOR;
	if (v > 1) return 1;
	return v;
}

/**
 * Apply forgetting curves in a single SQL pass. Uses a CASE expression to
 * map NodeKind -> half-life so a single query covers every kind. Emits
 * `memory.node.decayed` with a count + post-floor summary and returns
 * the number of rows affected.
 *
 * Pattern and principle nodes are excluded entirely (they are exempt).
 * Rows already at the floor are left alone — the UPDATE is bounded to
 * nodes whose decay would actually lower importance.
 */
export async function applyForgettingCurves(deps: ForgettingDeps): Promise<number> {
	const now = deps.now?.() ?? new Date();

	const result = await deps.sql.begin(async (tx) => {
		const q = asQueryable(tx);

		// Partition kinds by finite-vs-infinite half-life. Finite kinds feed
		// the CASE expression; exempt kinds are excluded by the WHERE clause.
		const finiteKinds = (Object.keys(HALF_LIFE_DAYS) as NodeKind[]).filter((k) =>
			Number.isFinite(HALF_LIFE_DAYS[k]),
		);
		const exemptKinds = (Object.keys(HALF_LIFE_DAYS) as NodeKind[]).filter(
			(k) => !Number.isFinite(HALF_LIFE_DAYS[k]),
		);

		// Build the CASE body as one composable SQL fragment. postgres.js
		// flattens nested tagged templates by substituting their parameters
		// in-order, so `sql`${fragment}`` stays correctly parameterized.
		let caseExpr = q`CASE kind`;
		for (const k of finiteKinds) {
			caseExpr = q`${caseExpr} WHEN ${k} THEN ${HALF_LIFE_DAYS[k]}::real`;
		}
		caseExpr = q`${caseExpr} ELSE ${BASE_HALF_LIFE_DAYS}::real END`;

		const rows = await q`
			WITH decayed AS (
				SELECT id,
					importance AS old_importance,
					GREATEST(
						${IMPORTANCE_FLOOR}::real,
						importance * power(
							0.5,
							EXTRACT(EPOCH FROM (${now}::timestamptz - COALESCE(last_accessed_at, created_at)))
								/ (86400.0
									* (${caseExpr})
									* (1 + access_count * ${ACCESS_HALF_LIFE_MULTIPLIER}::real))
						)
					)::real AS new_importance
				FROM node
				WHERE kind <> ALL(${exemptKinds})
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
