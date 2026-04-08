/**
 * User Model Repository.
 *
 * Manages the structured dimensions of the owner's profile. Each dimension
 * (communication_style, energy_patterns, etc.) has a confidence score computed
 * from evidence count divided by a per-category threshold. Thresholds live in
 * application code — changing them retroactively recomputes confidence for all
 * future reads without a migration.
 *
 * Every mutation emits a `memory.user_model.updated` event through the EventBus.
 */

import type { Sql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { Actor } from "../events/types.ts";
import type { JsonValue } from "./types.ts";

// ---------------------------------------------------------------------------
// Confidence Thresholds
// ---------------------------------------------------------------------------

/**
 * Per-dimension category thresholds.
 * Confidence = min(1.0, evidence_count / threshold).
 * Higher threshold = more evidence needed before Theo trusts its read.
 */
const CONFIDENCE_THRESHOLDS: Readonly<Record<string, number>> = {
	personality_type: 20,
	communication_style: 5,
	values: 15,
	energy_patterns: 10,
	boundaries: 3,
	cognitive_preferences: 8,
	shadow_patterns: 25,
	archetypes: 20,
	individuation_markers: 30,
};

const DEFAULT_THRESHOLD = 10;

/** Look up the evidence threshold for a dimension name. */
export function getThreshold(dimensionName: string): number {
	return CONFIDENCE_THRESHOLDS[dimensionName] ?? DEFAULT_THRESHOLD;
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface UserModelDimension {
	readonly id: number;
	readonly name: string;
	readonly value: JsonValue;
	readonly confidence: number;
	readonly evidenceCount: number;
	readonly threshold: number;
	readonly createdAt: Date;
	readonly updatedAt: Date;
}

/** Maps a raw postgres row (bracket-indexed) to a typed dimension. */
function rowToDimension(row: Record<string, unknown>): UserModelDimension {
	return {
		id: row["id"] as number,
		name: row["name"] as string,
		value: row["value"] as JsonValue,
		confidence: row["confidence"] as number,
		evidenceCount: row["evidence_count"] as number,
		threshold: getThreshold(row["name"] as string),
		createdAt: row["created_at"] as Date,
		updatedAt: row["updated_at"] as Date,
	};
}

// ---------------------------------------------------------------------------
// Repository
// ---------------------------------------------------------------------------

export interface UserModelRepository {
	getDimensions(): Promise<readonly UserModelDimension[]>;
	getDimension(name: string): Promise<UserModelDimension | null>;
	updateDimension(
		name: string,
		value: JsonValue,
		evidence: number,
		actor: Actor,
	): Promise<UserModelDimension>;
}

export function createUserModelRepository(sql: Sql, bus: EventBus): UserModelRepository {
	async function getDimensions(): Promise<readonly UserModelDimension[]> {
		const rows = await sql<Record<string, unknown>[]>`
			SELECT id, name, value, confidence, evidence_count, created_at, updated_at
			FROM user_model_dimension
			ORDER BY name
		`;
		return rows.map(rowToDimension);
	}

	async function getDimension(name: string): Promise<UserModelDimension | null> {
		const rows = await sql<Record<string, unknown>[]>`
			SELECT id, name, value, confidence, evidence_count, created_at, updated_at
			FROM user_model_dimension
			WHERE name = ${name}
		`;
		const row = rows[0];
		if (!row) return null;
		return rowToDimension(row);
	}

	async function updateDimension(
		name: string,
		value: JsonValue,
		evidence: number,
		actor: Actor,
	): Promise<UserModelDimension> {
		const threshold = getThreshold(name);

		// Value is replaced (latest observation wins); evidence_count accumulates.
		return sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q<Record<string, unknown>[]>`
				INSERT INTO user_model_dimension (name, value, evidence_count, confidence)
				VALUES (
					${name},
					${sql.json(value)},
					${evidence},
					LEAST(1.0, ${evidence}::real / ${threshold})
				)
				ON CONFLICT (name) DO UPDATE SET
					value = ${sql.json(value)},
					evidence_count = user_model_dimension.evidence_count + ${evidence},
					confidence = LEAST(1.0,
						(user_model_dimension.evidence_count + ${evidence})::real / ${threshold})
				RETURNING id, name, value, confidence, evidence_count, created_at, updated_at
			`;

			const row = rows[0];
			if (!row) throw new Error("RETURNING clause returned no rows");

			await bus.emit(
				{
					type: "memory.user_model.updated",
					version: 1,
					actor,
					data: { dimension: name, confidence: row["confidence"] as number },
					metadata: {},
				},
				{ tx },
			);

			return rowToDimension(row);
		});
	}

	return { getDimensions, getDimension, updateDimension };
}
