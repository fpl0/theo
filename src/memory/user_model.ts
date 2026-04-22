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
 *
 * Dimensions are grouped by evidentiary status:
 *
 *   - Big Five (openness, conscientiousness, extraversion, agreeableness,
 *     neuroticism): empirically grounded personality dimensions. Default
 *     thresholds (10-15) reflect broad research support.
 *   - Behavioral observables (communication_style, energy_patterns,
 *     boundaries, cognitive_preferences, values): direct evidence every
 *     turn. Low-to-moderate thresholds.
 *   - Jungian / depth-psychology dimensions (personality_type,
 *     shadow_patterns, archetypes, individuation_markers): experimental.
 *     They remain available for the agent to populate but require far
 *     more evidence (see EXPERIMENTAL_EVIDENCE_FLOOR) before they appear
 *     in the system prompt. Thresholds here are raised so an accidental
 *     early observation does not drive a high-confidence inclusion.
 */
const CONFIDENCE_THRESHOLDS: Readonly<Record<string, number>> = {
	// Big Five (empirically grounded)
	openness: 10,
	conscientiousness: 10,
	extraversion: 10,
	agreeableness: 10,
	neuroticism: 10,
	// Behavioral observables
	communication_style: 5,
	values: 15,
	energy_patterns: 10,
	boundaries: 3,
	cognitive_preferences: 8,
	// Depth-psychology (experimental)
	personality_type: 50,
	shadow_patterns: 50,
	archetypes: 50,
	individuation_markers: 50,
};

const DEFAULT_THRESHOLD = 10;

/** Look up the evidence threshold for a dimension name. */
export function getThreshold(dimensionName: string): number {
	return CONFIDENCE_THRESHOLDS[dimensionName] ?? DEFAULT_THRESHOLD;
}

// ---------------------------------------------------------------------------
// Experimental dimensions
// ---------------------------------------------------------------------------

/**
 * Dimensions whose theoretical basis is weaker (Jungian / depth psychology).
 * They are tracked internally but excluded from the system prompt until
 * their evidence count crosses EXPERIMENTAL_EVIDENCE_FLOOR.
 */
export const EXPERIMENTAL_DIMENSIONS: ReadonlySet<string> = new Set<string>([
	"personality_type",
	"shadow_patterns",
	"archetypes",
	"individuation_markers",
]);

/**
 * Evidence count an experimental dimension must reach before it is
 * included in the rendered prompt. Deliberately higher than the
 * standard thresholds -- these dimensions should only surface once
 * their pattern is unmistakable.
 */
export const EXPERIMENTAL_EVIDENCE_FLOOR = 50;

/**
 * Decide whether a dimension is ready to appear in the system prompt.
 * Non-experimental dimensions always qualify; experimental ones must
 * meet the floor.
 */
export function isDimensionPromptReady(name: string, evidenceCount: number): boolean {
	if (!EXPERIMENTAL_DIMENSIONS.has(name)) return true;
	return evidenceCount >= EXPERIMENTAL_EVIDENCE_FLOOR;
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type EgressSensitivity = "public" | "private" | "local_only";

export interface UserModelDimension {
	readonly id: number;
	readonly name: string;
	readonly value: JsonValue;
	readonly confidence: number;
	readonly evidenceCount: number;
	readonly threshold: number;
	readonly egressSensitivity: EgressSensitivity;
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
		egressSensitivity: (row["egress_sensitivity"] as EgressSensitivity | undefined) ?? "private",
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
			SELECT id, name, value, confidence, evidence_count, egress_sensitivity, created_at, updated_at
			FROM user_model_dimension
			ORDER BY name
		`;
		return rows.map(rowToDimension);
	}

	async function getDimension(name: string): Promise<UserModelDimension | null> {
		const rows = await sql<Record<string, unknown>[]>`
			SELECT id, name, value, confidence, evidence_count, egress_sensitivity, created_at, updated_at
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
