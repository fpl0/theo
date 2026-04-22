/**
 * Goal lease — single-runner-per-goal guarantee.
 *
 * Acquisition is atomic via `SELECT FOR UPDATE SKIP LOCKED + UPDATE` inside
 * a transaction. The winning runner emits `goal.lease_acquired` in the same
 * transaction, so the projection's lease fields and the event log stay
 * consistent. `SKIP LOCKED` ensures contending runners return null instead
 * of blocking.
 *
 * Priority aging: the ORDER BY mixes `owner_priority DESC` with a term that
 * promotes goals that haven't been worked recently. `AGING_WEEKLY_BONUS`
 * points per week of staleness. See plan §9.
 */

import type { Sql, TransactionSql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { NodeId } from "../memory/graph/types.ts";
import { asNodeId } from "../memory/graph/types.ts";
import type { GoalRepository } from "./repository.ts";
import type { GoalState } from "./types.ts";
import {
	AGING_WEEKLY_BONUS,
	DEFAULT_LEASE_DURATION_MS,
	type GoalRunnerId,
	type LeaseReleaseReason,
} from "./types.ts";

// ---------------------------------------------------------------------------
// Dependencies
// ---------------------------------------------------------------------------

export interface LeaseDeps {
	readonly sql: Sql;
	readonly bus: EventBus;
	readonly goals: GoalRepository;
	readonly leaseDurationMs?: number;
	readonly agingWeeklyBonus?: number;
	readonly now?: () => Date;
}

export interface AcquireOptions {
	readonly statuses?: readonly ("active" | "blocked")[];
}

/**
 * Result of a successful lease acquisition. The caller uses these fields to
 * build the execution context without re-reading the projection.
 */
export interface LeasedGoal {
	readonly state: GoalState;
	readonly runnerId: GoalRunnerId;
	readonly leaseDurationMs: number;
}

// ---------------------------------------------------------------------------
// Lease
// ---------------------------------------------------------------------------

export class GoalLease {
	private readonly sql: Sql;
	private readonly bus: EventBus;
	private readonly goals: GoalRepository;
	private readonly leaseDurationMs: number;
	private readonly agingBonus: number;
	private readonly now: () => Date;

	constructor(deps: LeaseDeps) {
		this.sql = deps.sql;
		this.bus = deps.bus;
		this.goals = deps.goals;
		this.leaseDurationMs = deps.leaseDurationMs ?? DEFAULT_LEASE_DURATION_MS;
		this.agingBonus = deps.agingWeeklyBonus ?? AGING_WEEKLY_BONUS;
		this.now = deps.now ?? ((): Date => new Date());
	}

	/**
	 * Atomically acquire a lease on the highest-priority eligible goal.
	 * Returns null when no eligible goal is available (or all are locked by
	 * other runners). Emits `goal.lease_acquired` on success.
	 */
	async acquire(runnerId: GoalRunnerId, opts: AcquireOptions = {}): Promise<LeasedGoal | null> {
		const statuses = (opts.statuses ?? ["active"]) as unknown as string[];
		const now = this.now();
		const leasedUntil = new Date(now.getTime() + this.leaseDurationMs);
		const agingBonus = this.agingBonus;

		const selected = await this.sql.begin(async (tx) => {
			const q = asQueryable(tx);
			// SKIP LOCKED + FOR UPDATE gives us the single-runner guarantee
			// without blocking contending runners. Aging is summed into the
			// effective priority (not a secondary sort key) so that a
			// stale-but-low goal can overtake a fresh higher-priority one
			// after enough staleness.
			const updated = await q<{ nodeId: number }[]>`
				WITH eligible AS (
					SELECT node_id FROM goal_state
					WHERE status = ANY(${statuses}::text[])
					  AND redacted = false
					  AND (leased_by IS NULL OR leased_until < ${now})
					ORDER BY
						(
							owner_priority
							+ LEAST(100,
								EXTRACT(EPOCH FROM (${now} - COALESCE(last_worked_at, created_at)))
									/ 604800.0 * ${agingBonus}
							)
						) DESC,
						created_at ASC
					LIMIT 1
					FOR UPDATE SKIP LOCKED
				)
				UPDATE goal_state
				SET leased_by = ${runnerId},
					leased_until = ${leasedUntil}
				FROM eligible
				WHERE goal_state.node_id = eligible.node_id
				RETURNING goal_state.node_id AS "nodeId"
			`;
			const row = updated[0];
			if (row === undefined) return null;

			// Emit the lease event in the same transaction so the projection
			// and event log commit together. A takeover from another runner
			// is reconciled by the projection's `applyLeaseAcquired`.
			await this.bus.emit(
				{
					type: "goal.lease_acquired",
					version: 1,
					actor: "system",
					data: {
						nodeId: row.nodeId,
						runnerId,
						leaseDurationMs: this.leaseDurationMs,
					},
					metadata: {},
				},
				{ tx },
			);
			return asNodeId(row.nodeId);
		});

		if (selected === null) return null;
		await this.bus.flush();

		const state = await this.goals.readState(selected);
		if (state === null) {
			throw new Error(
				`GoalLease.acquire: projection missing for node ${String(selected)} after lease`,
			);
		}
		return {
			state,
			runnerId,
			leaseDurationMs: this.leaseDurationMs,
		};
	}

	/** Renew a lease held by this runner. Emits `goal.lease_acquired` (idempotent for the projection). */
	async heartbeat(nodeId: NodeId, runnerId: GoalRunnerId): Promise<boolean> {
		const now = this.now();
		const leasedUntil = new Date(now.getTime() + this.leaseDurationMs);
		const renewed = await this.sql.begin(async (tx) => {
			const q = asQueryable(tx);
			const rows = await q<{ nodeId: number }[]>`
				UPDATE goal_state
				SET leased_until = ${leasedUntil}
				WHERE node_id = ${Number(nodeId)}
				  AND leased_by = ${runnerId}
				  AND leased_until > ${now}
				RETURNING node_id AS "nodeId"
			`;
			if (rows.length === 0) return false;
			await this.bus.emit(
				{
					type: "goal.lease_acquired",
					version: 1,
					actor: "system",
					data: {
						nodeId: Number(nodeId),
						runnerId,
						leaseDurationMs: this.leaseDurationMs,
					},
					metadata: {},
				},
				{ tx },
			);
			return true;
		});
		await this.bus.flush();
		return renewed;
	}

	/** Release the lease. Emits `goal.lease_released`. */
	async release(
		nodeId: NodeId,
		runnerId: GoalRunnerId,
		reason: LeaseReleaseReason = "normal",
	): Promise<void> {
		await this.sql.begin(async (tx) => {
			await this.bus.emit(
				{
					type: "goal.lease_released",
					version: 1,
					actor: "system",
					data: {
						nodeId: Number(nodeId),
						runnerId,
						reason,
					},
					metadata: {},
				},
				{ tx },
			);
		});
		await this.bus.flush();
	}

	/** Expose the configured lease duration — used by the executive loop. */
	get durationMs(): number {
		return this.leaseDurationMs;
	}
}

// Helper re-export so callers don't need a direct tx import.
export type { TransactionSql };
