/**
 * Self Model Repository.
 *
 * Tracks Theo's prediction accuracy per task domain with both
 * lifetime-cumulative and windowed counters:
 *
 *   1. recordPrediction() -- called when Theo makes a prediction.
 *      Increments both predictions and recent_predictions. If the window
 *      is due to reset (30 days elapsed or 50 recent predictions), the
 *      windowed counters are zeroed first and the window timer restarts.
 *   2. recordOutcome() -- called when the outcome is known. Increments
 *      correct and recent_correct if the prediction was right. Emits a
 *      memory.self_model.updated event carrying the primary (windowed)
 *      calibration.
 *
 * Windowed calibration = recent_correct / recent_predictions. It is the
 * primary signal for autonomy graduation (Phase 12a) -- lifetime
 * calibration eventually becomes unresponsive to recent performance
 * after tens of thousands of predictions. Lifetime remains available
 * via `getLifetimeCalibration()` as a secondary sanity check.
 *
 * Domains: scheduling, drafting, recommendations, memory_relevance,
 * goal_planning, mood_assessment, session_management.
 */

import type { Sql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { Actor } from "../events/types.ts";

// ---------------------------------------------------------------------------
// Tunables
// ---------------------------------------------------------------------------

/** Window duration in days. The window resets on the next prediction past this age. */
export const WINDOW_DAYS = 30;

/**
 * Max predictions inside a window before it resets. Prevents a burst of
 * activity from producing calibration over a narrow timeslice.
 */
export const WINDOW_MAX_PREDICTIONS = 50;

const WINDOW_MS = WINDOW_DAYS * 86_400 * 1000;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface SelfModelDomain {
	readonly id: number;
	readonly name: string;
	readonly predictions: number;
	readonly correct: number;
	readonly recentPredictions: number;
	readonly recentCorrect: number;
	readonly windowResetAt: Date;
	readonly createdAt: Date;
	readonly updatedAt: Date;
}

/** Maps a raw postgres row (bracket-indexed) to a typed domain. */
function rowToDomain(row: Record<string, unknown>): SelfModelDomain {
	return {
		id: row["id"] as number,
		name: row["name"] as string,
		predictions: row["predictions"] as number,
		correct: row["correct"] as number,
		recentPredictions: row["recent_predictions"] as number,
		recentCorrect: row["recent_correct"] as number,
		windowResetAt: row["window_reset_at"] as Date,
		createdAt: row["created_at"] as Date,
		updatedAt: row["updated_at"] as Date,
	};
}

// ---------------------------------------------------------------------------
// Repository
// ---------------------------------------------------------------------------

export interface SelfModelRepository {
	recordPrediction(domain: string, actor: Actor): Promise<void>;
	recordOutcome(domain: string, correct: boolean, actor: Actor): Promise<void>;
	/** Windowed calibration (recent_correct / recent_predictions). */
	getCalibration(domain: string): Promise<number>;
	/** Lifetime-cumulative calibration (correct / predictions). */
	getLifetimeCalibration(domain: string): Promise<number>;
	getDomain(domain: string): Promise<SelfModelDomain | null>;
}

interface Counters {
	readonly predictions: number;
	readonly correct: number;
	readonly recentPredictions: number;
	readonly recentCorrect: number;
}

function windowedCalibration(row: Counters | undefined): number {
	if (!row || row.recentPredictions === 0) return 0;
	return row.recentCorrect / row.recentPredictions;
}

function lifetimeCalibration(row: Counters | undefined): number {
	if (!row || row.predictions === 0) return 0;
	return row.correct / row.predictions;
}

export function createSelfModelRepository(sql: Sql, bus: EventBus): SelfModelRepository {
	async function getCalibration(domain: string): Promise<number> {
		const rows = await sql<Counters[]>`
			SELECT predictions, correct,
				recent_predictions AS "recentPredictions",
				recent_correct AS "recentCorrect"
			FROM self_model_domain WHERE name = ${domain}
		`;
		return windowedCalibration(rows[0]);
	}

	async function getLifetimeCalibration(domain: string): Promise<number> {
		const rows = await sql<Counters[]>`
			SELECT predictions, correct,
				recent_predictions AS "recentPredictions",
				recent_correct AS "recentCorrect"
			FROM self_model_domain WHERE name = ${domain}
		`;
		return lifetimeCalibration(rows[0]);
	}

	async function getDomain(domain: string): Promise<SelfModelDomain | null> {
		const rows = await sql<Record<string, unknown>[]>`
			SELECT id, name, predictions, correct,
				recent_predictions, recent_correct, window_reset_at,
				created_at, updated_at
			FROM self_model_domain
			WHERE name = ${domain}
		`;
		const row = rows[0];
		if (!row) return null;
		return rowToDomain(row);
	}

	async function recordPrediction(domain: string, actor: Actor): Promise<void> {
		// RETURNING avoids a second query for calibration (no TOCTOU race).
		// The CASE expressions encode the window-reset rule: if the stored
		// window is older than WINDOW_MS or contains >= WINDOW_MAX predictions
		// BEFORE this increment, the window starts fresh. The counters on
		// conflict read `self_model_domain.*` columns so the decision is
		// made against pre-increment values.
		await sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q<Counters[]>`
				INSERT INTO self_model_domain (name, predictions, recent_predictions, window_reset_at)
				VALUES (${domain}, 1, 1, now())
				ON CONFLICT (name) DO UPDATE SET
					predictions = self_model_domain.predictions + 1,
					recent_predictions = CASE
						WHEN self_model_domain.window_reset_at < now() - (${WINDOW_DAYS}::bigint * interval '1 day')
						  OR self_model_domain.recent_predictions >= ${WINDOW_MAX_PREDICTIONS}
						THEN 1
						ELSE self_model_domain.recent_predictions + 1
					END,
					recent_correct = CASE
						WHEN self_model_domain.window_reset_at < now() - (${WINDOW_DAYS}::bigint * interval '1 day')
						  OR self_model_domain.recent_predictions >= ${WINDOW_MAX_PREDICTIONS}
						THEN 0
						ELSE self_model_domain.recent_correct
					END,
					window_reset_at = CASE
						WHEN self_model_domain.window_reset_at < now() - (${WINDOW_DAYS}::bigint * interval '1 day')
						  OR self_model_domain.recent_predictions >= ${WINDOW_MAX_PREDICTIONS}
						THEN now()
						ELSE self_model_domain.window_reset_at
					END
				RETURNING predictions, correct,
				recent_predictions AS "recentPredictions",
				recent_correct AS "recentCorrect"
			`;

			await bus.emit(
				{
					type: "memory.self_model.updated",
					version: 1,
					actor,
					data: { domain, calibration: windowedCalibration(rows[0]) },
					metadata: {},
				},
				{ tx },
			);
		});
	}

	async function recordOutcome(domain: string, correct: boolean, actor: Actor): Promise<void> {
		// The window may have rolled over via prune time since the matching
		// prediction fired. We still credit recent_correct if the window is
		// current (no reset owed); if the window has already rolled, only the
		// lifetime counter moves. This avoids double-counting inside a fresh
		// window and keeps the windowed ratio honest.
		await sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q<Counters[]>`
				UPDATE self_model_domain
				SET correct = self_model_domain.correct + CASE WHEN ${correct} THEN 1 ELSE 0 END,
				    recent_correct = self_model_domain.recent_correct + CASE
				      WHEN ${correct}
				        AND self_model_domain.window_reset_at
				          >= now() - (${WINDOW_DAYS}::bigint * interval '1 day')
				      THEN 1
				      ELSE 0
				    END
				WHERE name = ${domain}
				RETURNING predictions, correct,
				recent_predictions AS "recentPredictions",
				recent_correct AS "recentCorrect"
			`;
			if (rows.length === 0) {
				throw new Error(`Self model domain '${domain}' not found`);
			}

			await bus.emit(
				{
					type: "memory.self_model.updated",
					version: 1,
					actor,
					data: { domain, correct, calibration: windowedCalibration(rows[0]) },
					metadata: {},
				},
				{ tx },
			);
		});
	}

	return {
		recordPrediction,
		recordOutcome,
		getCalibration,
		getLifetimeCalibration,
		getDomain,
	};
}

// ---------------------------------------------------------------------------
// Exported helpers for tests
// ---------------------------------------------------------------------------

/** Helper used in tests: does the stored timestamp indicate a window reset is due? */
export function isWindowDue(
	windowResetAt: Date,
	recentPredictions: number,
	now: Date = new Date(),
): boolean {
	if (recentPredictions >= WINDOW_MAX_PREDICTIONS) return true;
	return now.getTime() - windowResetAt.getTime() >= WINDOW_MS;
}
