/**
 * Self Model Repository.
 *
 * Tracks Theo's prediction accuracy per task domain. The flow has two steps:
 *
 *   1. recordPrediction() — called when Theo makes a prediction. Increments
 *      the predictions counter. This is the commitment: "I think X will happen."
 *   2. recordOutcome() — called when the outcome is known. If correct=true,
 *      increments the correct counter. Either way, emits a
 *      memory.self_model.updated event for the audit trail.
 *
 * Calibration = correct / predictions. A well-calibrated agent has calibration
 * close to its stated confidence levels.
 *
 * Domains: scheduling, drafting, recommendations, memory_relevance,
 * goal_planning, mood_assessment, session_management.
 */

import type { Sql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { Actor } from "../events/types.ts";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface SelfModelDomain {
	readonly id: number;
	readonly name: string;
	readonly predictions: number;
	readonly correct: number;
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
	getCalibration(domain: string): Promise<number>;
	getDomain(domain: string): Promise<SelfModelDomain | null>;
}

interface Counters {
	readonly predictions: number;
	readonly correct: number;
}

function calibrationOf(row: Counters | undefined): number {
	if (!row || row.predictions === 0) return 0;
	return row.correct / row.predictions;
}

export function createSelfModelRepository(sql: Sql, bus: EventBus): SelfModelRepository {
	async function getCalibration(domain: string): Promise<number> {
		const rows = await sql<Counters[]>`
			SELECT predictions, correct FROM self_model_domain WHERE name = ${domain}
		`;
		return calibrationOf(rows[0]);
	}

	async function getDomain(domain: string): Promise<SelfModelDomain | null> {
		const rows = await sql<Record<string, unknown>[]>`
			SELECT id, name, predictions, correct, created_at, updated_at
			FROM self_model_domain
			WHERE name = ${domain}
		`;
		const row = rows[0];
		if (!row) return null;
		return rowToDomain(row);
	}

	async function recordPrediction(domain: string, actor: Actor): Promise<void> {
		// RETURNING avoids a second query for calibration (no TOCTOU race).
		await sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q<Counters[]>`
				INSERT INTO self_model_domain (name, predictions)
				VALUES (${domain}, 1)
				ON CONFLICT (name) DO UPDATE SET
					predictions = self_model_domain.predictions + 1
				RETURNING predictions, correct
			`;

			await bus.emit(
				{
					type: "memory.self_model.updated",
					version: 1,
					actor,
					data: { domain, calibration: calibrationOf(rows[0]) },
					metadata: {},
				},
				{ tx },
			);
		});
	}

	async function recordOutcome(domain: string, correct: boolean, actor: Actor): Promise<void> {
		// Single UPDATE with conditional increment — correct outcomes add 1,
		// incorrect outcomes add 0. Both verify the domain exists via RETURNING.
		await sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q<Counters[]>`
				UPDATE self_model_domain
				SET correct = self_model_domain.correct + CASE WHEN ${correct} THEN 1 ELSE 0 END
				WHERE name = ${domain}
				RETURNING predictions, correct
			`;
			if (rows.length === 0) {
				throw new Error(`Self model domain '${domain}' not found`);
			}

			await bus.emit(
				{
					type: "memory.self_model.updated",
					version: 1,
					actor,
					data: { domain, correct, calibration: calibrationOf(rows[0]) },
					metadata: {},
				},
				{ tx },
			);
		});
	}

	return { recordPrediction, recordOutcome, getCalibration, getDomain };
}
