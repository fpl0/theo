/**
 * Degradation ladder (`foundation.md §7.5`).
 *
 * Five levels (0-4). The degradation state is a singleton row in
 * `degradation_state` projected from `degradation.level_changed` events.
 *
 *   L0 — healthy: all classes run; advisor enabled.
 *   L1 — elevated: advisor dropped from ideation only.
 *   L2 — constrained: ideation skipped; reflex + executive continue.
 *   L3 — critical: only reflex + interactive; executive paused.
 *   L4 — essential: only interactive.
 *
 * The CLI command `/degradation` reads the current level via
 * `readDegradation()`. The engine lifecycle adjusts the level via
 * `setDegradation()` based on cost/error heuristics (Phase 15 wiring).
 */

import type { Sql, TransactionSql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { Actor } from "../events/types.ts";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type DegradationLevel = 0 | 1 | 2 | 3 | 4;

export interface DegradationState {
	readonly level: DegradationLevel;
	readonly reason: string;
	readonly changedAt: Date;
}

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

function coerceLevel(level: number): DegradationLevel {
	if (level <= 0) return 0;
	if (level >= 4) return 4;
	// All integers 0..4 satisfy the branded type at runtime; the `as`
	// narrows the numeric type safely since we just clamped it.
	return level as DegradationLevel;
}

/** Read the singleton degradation state. */
export async function readDegradation(sql: Sql | TransactionSql): Promise<DegradationState> {
	const query = asQueryable(sql);
	const rows = await query<Record<string, unknown>[]>`
		SELECT level, reason, changed_at FROM degradation_state WHERE id = 'singleton'
	`;
	const row = rows[0];
	if (!row) {
		return { level: 0, reason: "initial", changedAt: new Date(0) };
	}
	return {
		level: coerceLevel(row["level"] as number),
		reason: row["reason"] as string,
		changedAt: row["changed_at"] as Date,
	};
}

// ---------------------------------------------------------------------------
// Command
// ---------------------------------------------------------------------------

/**
 * Change the degradation level. Idempotent when `newLevel` matches the
 * current value — emits nothing and returns the unchanged state.
 *
 * Emits `degradation.level_changed` and updates the singleton projection in
 * one transaction so the row and the event stay in sync under replay.
 */
export async function setDegradation(
	deps: { readonly sql: Sql; readonly bus: EventBus },
	newLevel: DegradationLevel,
	reason: string,
	actor: Actor = "system",
): Promise<DegradationState> {
	return deps.sql.begin(async (tx) => {
		const current = await readDegradation(tx);
		if (current.level === newLevel) return current;
		await deps.bus.emit(
			{
				type: "degradation.level_changed",
				version: 1,
				actor,
				data: {
					previousLevel: current.level,
					newLevel,
					reason,
				},
				metadata: {},
			},
			{ tx },
		);
		const q = asQueryable(tx);
		await q`
			UPDATE degradation_state
			SET level = ${newLevel},
			    reason = ${reason},
			    changed_at = now()
			WHERE id = 'singleton'
		`;
		return { level: newLevel, reason, changedAt: new Date() };
	});
}

// ---------------------------------------------------------------------------
// Policy helpers (tested pure functions)
// ---------------------------------------------------------------------------

/**
 * Is the ideation class allowed to run at this level? Ideation stops at L2.
 */
export function ideationAllowed(level: DegradationLevel): boolean {
	return level <= 1;
}

/** Is the advisor allowed at this level? Dropped from ideation at L1; from all autonomous at L2. */
export function advisorAllowed(level: DegradationLevel, turnClass: string): boolean {
	if (turnClass === "interactive") return level <= 3;
	if (turnClass === "ideation") return level === 0;
	// reflex / executive
	return level <= 1;
}

/** Is the executive class allowed? Paused at L3. */
export function executiveAllowed(level: DegradationLevel): boolean {
	return level <= 2;
}

/** Is the reflex class allowed? Paused at L4. */
export function reflexAllowed(level: DegradationLevel): boolean {
	return level <= 3;
}
