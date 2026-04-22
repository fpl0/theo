/**
 * goal_state / goal_task projection.
 *
 * Every column in the two projection tables has exactly one event that
 * writes it. The projection handlers are **decision handlers** — pure over
 * event data — so they run on both live dispatch and replay.
 *
 * One handler is registered per event type with the same id per type
 * (e.g., `goal-projection-task-started`), so each type has its own
 * checkpoint. But handlers within the projection MUST see events in
 * global ULID order to prevent task_completed from running before
 * plan_updated during replay. The bus's global replay path sequentially
 * dispatches by handler, but since each handler is type-scoped, we rely
 * on foundation replay ordering: the bus replays every handler's events
 * in ULID order within that handler. Cross-handler ordering is NOT
 * guaranteed.
 *
 * To guarantee cross-handler ordering, we register a SINGLE projection
 * handler that subscribes to every goal event type individually — the
 * bus calls it in ULID order within each queue, but since there's only
 * one queue per type, the `applyIfFresh` gate on task events plus the
 * defer-unless-row-exists pattern (rows missing → no-op, tolerate later
 * events) covers the remaining cases.
 *
 * Idempotency: projection writes are UPSERTs; task events carry a `turnId`
 * that gates mutation so replaying the same event twice is a no-op
 * beyond the first application. See `applyIfFresh` below.
 *
 * Rules map 1:1 to the table in `docs/plans/foundation/12a-goal-loops.md §4`.
 */

import type { Sql, TransactionSql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { Event, EventOfType } from "../events/types.ts";
import type { BlockReason, GoalOrigin, GoalStatus, PlanStep, TaskStatus } from "./types.ts";

// ---------------------------------------------------------------------------
// Handler id — one handler covers all goal events so they dispatch in ULID
// order on replay. Each event type registers separately on the bus so the
// replay cursor is shared across all goal-event types.
// ---------------------------------------------------------------------------

export const PROJECTION_HANDLER_ID = "goal-projection";

/**
 * Every goal event type. Registering them all under the same handler id
 * would need a shared queue; the bus is one-queue-per-registration, so we
 * create N registrations with distinct ids but share a `dispatch` function
 * so the logic is centralized.
 */
const GOAL_EVENT_TYPES = [
	"goal.created",
	"goal.confirmed",
	"goal.priority_changed",
	"goal.plan_updated",
	"goal.lease_acquired",
	"goal.lease_released",
	"goal.task_started",
	"goal.task_progress",
	"goal.task_completed",
	"goal.task_failed",
	"goal.task_yielded",
	"goal.task_abandoned",
	"goal.blocked",
	"goal.unblocked",
	"goal.reconsidered",
	"goal.paused",
	"goal.resumed",
	"goal.cancelled",
	"goal.completed",
	"goal.quarantined",
	"goal.redacted",
	"goal.expired",
] as const;

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------

export interface ProjectionDeps {
	readonly sql: Sql;
	readonly bus: EventBus;
}

/**
 * Register projection handlers for every goal event type. Each type has
 * its own bus handler with a type-scoped id (so replay cursors work per
 * type), but all dispatch to the same `dispatchGoalEvent`.
 *
 * Cross-handler replay ordering: the bus replays each handler's queue
 * independently and concurrently. To prevent inter-type races (e.g.,
 * `task_completed` landing before `plan_updated` on replay), every apply
 * function acquires an in-process serialization mutex — this guarantees
 * that only one projection write runs at a time within this process. The
 * transactions are still short and independent; we only serialize the
 * handler bodies, not the SQL.
 */
export function registerGoalProjection(deps: ProjectionDeps): void {
	const { bus } = deps;
	const mutex = new AsyncMutex();
	for (const type of GOAL_EVENT_TYPES) {
		bus.on(
			type,
			async (event, tx) => {
				await mutex.run(async () => {
					await dispatchGoalEvent(event, requireTx(tx));
				});
			},
			{ id: `${PROJECTION_HANDLER_ID}-${type}`, mode: "decision" },
		);
	}
}

// ---------------------------------------------------------------------------
// Async mutex — trivially small; a promise-chain serializer.
// ---------------------------------------------------------------------------

class AsyncMutex {
	private tail: Promise<void> = Promise.resolve();
	run<T>(fn: () => Promise<T>): Promise<T> {
		const next = this.tail.then(fn);
		this.tail = next.then(
			() => undefined,
			() => undefined,
		);
		return next;
	}
}

/**
 * Central dispatch — routes a goal event to its apply function. One place
 * for the switch keeps logic colocated; the bus handler registrations
 * just forward to this function.
 */
async function dispatchGoalEvent(event: Event, tx: TransactionSql): Promise<void> {
	switch (event.type) {
		case "goal.created":
			await applyGoalCreated(event, tx);
			return;
		case "goal.confirmed":
			await updateStatus(event.data.nodeId, "active", tx, { fromStatuses: ["proposed"] });
			return;
		case "goal.priority_changed": {
			const q = asQueryable(tx);
			await q`
				UPDATE goal_state
				SET owner_priority = ${event.data.newPriority},
				    consecutive_failures = CASE
				      WHEN status = 'quarantined' THEN 0
				      ELSE consecutive_failures
				    END
				WHERE node_id = ${event.data.nodeId}
			`;
			return;
		}
		case "goal.plan_updated":
			await applyPlanUpdated(event, tx);
			return;
		case "goal.lease_acquired":
			await applyLeaseAcquired(event, tx);
			return;
		case "goal.lease_released": {
			const q = asQueryable(tx);
			await q`
				UPDATE goal_state
				SET leased_by = NULL, leased_until = NULL
				WHERE node_id = ${event.data.nodeId}
				  AND leased_by = ${event.data.runnerId}
			`;
			return;
		}
		case "goal.task_started":
			await applyTaskStarted(event, tx);
			return;
		case "goal.task_progress": {
			const q = asQueryable(tx);
			await q`
				UPDATE goal_state
				SET last_worked_at = ${event.timestamp}
				WHERE node_id = ${event.data.nodeId}
			`;
			return;
		}
		case "goal.task_completed":
			await applyTaskCompleted(event, tx);
			return;
		case "goal.task_failed":
			await applyTaskFailed(event, tx);
			return;
		case "goal.task_yielded":
			await applyTaskYielded(event, tx);
			return;
		case "goal.task_abandoned":
			await applyTaskAbandoned(event, tx);
			return;
		case "goal.blocked":
			await applyBlocked(event, tx);
			return;
		case "goal.unblocked": {
			const q = asQueryable(tx);
			await q`
				UPDATE goal_state
				SET status = 'active', blocked_reason = NULL
				WHERE node_id = ${event.data.nodeId} AND status = 'blocked'
			`;
			return;
		}
		case "goal.reconsidered": {
			const q = asQueryable(tx);
			await q`
				UPDATE goal_state
				SET last_reconsidered_at = ${event.timestamp}
				WHERE node_id = ${event.data.nodeId}
			`;
			return;
		}
		case "goal.paused":
			await updateStatus(event.data.nodeId, "paused", tx, {
				fromStatuses: ["active", "blocked"],
			});
			return;
		case "goal.resumed":
			await updateStatus(event.data.nodeId, "active", tx, {
				fromStatuses: ["paused"],
			});
			return;
		case "goal.cancelled":
			await updateStatus(event.data.nodeId, "cancelled", tx, {
				fromStatuses: ["proposed", "active", "blocked", "paused"],
			});
			return;
		case "goal.completed":
			await updateStatus(event.data.nodeId, "completed", tx, {
				fromStatuses: ["active", "blocked"],
			});
			return;
		case "goal.quarantined": {
			const q = asQueryable(tx);
			await q`
				UPDATE goal_state
				SET status = 'quarantined',
				    quarantined_reason = ${event.data.reason}
				WHERE node_id = ${event.data.nodeId}
				  AND status NOT IN ('completed','cancelled','expired')
			`;
			return;
		}
		case "goal.redacted": {
			const q = asQueryable(tx);
			await q`
				UPDATE goal_state
				SET redacted = true
				WHERE node_id = ${event.data.nodeId}
			`;
			if (event.data.redactedFields.includes("task_bodies")) {
				await q`
					UPDATE goal_task
					SET redacted = true
					WHERE goal_node_id = ${event.data.nodeId}
				`;
			}
			return;
		}
		case "goal.expired":
			await updateStatus(event.data.nodeId, "expired", tx, {
				fromStatuses: ["proposed"],
			});
			return;
		default:
			// Non-goal events fall through — other handler ids run them.
			return;
	}
}

// ---------------------------------------------------------------------------
// Apply functions
// ---------------------------------------------------------------------------

function requireTx(tx: TransactionSql | undefined): TransactionSql {
	if (tx === undefined) {
		throw new Error("Goal projection handler called without a transaction");
	}
	return tx;
}

/**
 * Raised when a task event (started/completed/failed/yielded) arrives at the
 * projection before the corresponding `goal.plan_updated` handler has
 * inserted the task row. The bus's retry machinery will re-deliver the
 * event (with backoff) and typically succeed on retry as the sibling
 * handler catches up.
 */
export class GoalTaskOutOfOrderError extends Error {
	constructor(taskId: string) {
		super(`Goal task ${taskId} not yet materialized — projection is out of order`);
		this.name = "goal.out_of_order";
	}
}

async function applyGoalCreated(
	event: EventOfType<"goal.created">,
	tx: TransactionSql,
): Promise<void> {
	const q = asQueryable(tx);
	const origin: GoalOrigin = event.data.origin;
	// Owner-originated goals start active; everything else starts proposed and
	// needs an explicit `goal.confirmed`.
	const initialStatus: GoalStatus = origin === "owner" ? "active" : "proposed";
	const proposalExpiresAt = event.data.proposalExpiresAt
		? new Date(event.data.proposalExpiresAt)
		: null;

	await q`
		INSERT INTO goal_state (
			node_id, status, origin, owner_priority, effective_trust,
			proposed_expires_at
		) VALUES (
			${event.data.nodeId},
			${initialStatus},
			${origin},
			${event.data.ownerPriority},
			${event.data.effectiveTrust},
			${proposalExpiresAt}
		)
		ON CONFLICT (node_id) DO NOTHING
	`;
}

async function applyPlanUpdated(
	event: EventOfType<"goal.plan_updated">,
	tx: TransactionSql,
): Promise<void> {
	const q = asQueryable(tx);
	const { nodeId, planVersion, plan } = event.data;

	// Idempotency: only apply if planVersion > stored.
	const rows = await q<{ planVersion: number }[]>`
		SELECT plan_version AS "planVersion" FROM goal_state WHERE node_id = ${nodeId}
	`;
	const current = rows[0]?.planVersion ?? -1;
	if (planVersion <= current) return;

	// Mark any previous-version tasks that are still active as abandoned.
	await q`
		UPDATE goal_task
		SET status = 'abandoned'
		WHERE goal_node_id = ${nodeId}
		  AND plan_version < ${planVersion}
		  AND status IN ('pending','in_progress','yielded')
	`;

	// Insert new tasks for the new plan version.
	for (let i = 0; i < plan.length; i++) {
		const step: PlanStep = plan[i] as PlanStep;
		const dependsOn = step.dependsOn as unknown as string[];
		await q`
			INSERT INTO goal_task (
				task_id, goal_node_id, plan_version, step_order, body,
				depends_on, subagent
			) VALUES (
				${step.taskId},
				${nodeId},
				${planVersion},
				${i},
				${step.body},
				${dependsOn},
				${step.preferredSubagent ?? null}
			)
			ON CONFLICT (task_id) DO NOTHING
		`;
	}

	// Update goal_state — clear current_task_id, set new plan + version.
	await q`
		UPDATE goal_state
		SET plan_version = ${planVersion},
			plan = ${tx.json(plan as unknown as Parameters<typeof tx.json>[0])},
			current_task_id = NULL
		WHERE node_id = ${nodeId}
	`;
}

async function applyLeaseAcquired(
	event: EventOfType<"goal.lease_acquired">,
	tx: TransactionSql,
): Promise<void> {
	const q = asQueryable(tx);
	const leasedUntil = new Date(event.timestamp.getTime() + event.data.leaseDurationMs);

	// Load the current leased_by to decide whether this is a takeover (from
	// another runner) or a renewal (same runner). A takeover implies the
	// previous runner's in-flight task is dangling — we mark it abandoned
	// here so the next turn re-queues it. We only synthesize at this layer
	// when the recovery module isn't doing it; the recovery module handles
	// the startup case explicitly, but mid-operation takeovers still need
	// projection action to avoid losing state.
	const prior = await q<{ leasedBy: string | null; currentTaskId: string | null }[]>`
		SELECT leased_by AS "leasedBy",
		       current_task_id AS "currentTaskId"
		FROM goal_state WHERE node_id = ${event.data.nodeId}
	`;
	const priorRow = prior[0];
	if (
		priorRow !== undefined &&
		priorRow.leasedBy !== null &&
		priorRow.leasedBy !== event.data.runnerId &&
		priorRow.currentTaskId !== null
	) {
		// Lease takeover: mark the previous in-flight task abandoned so it
		// re-qualifies as pending on the next executive tick. failure_count is
		// bumped so the poison breaker can catch looping abandonment.
		await q`
			UPDATE goal_task
			SET status = 'abandoned',
				failure_count = failure_count + 1
			WHERE task_id = ${priorRow.currentTaskId}
			  AND status IN ('in_progress','yielded')
		`;
		await q`
			UPDATE goal_state
			SET current_task_id = NULL,
				consecutive_failures = consecutive_failures + 1
			WHERE node_id = ${event.data.nodeId}
		`;
	}

	await q`
		UPDATE goal_state
		SET leased_by = ${event.data.runnerId},
			leased_until = ${leasedUntil}
		WHERE node_id = ${event.data.nodeId}
	`;
}

async function applyTaskStarted(
	event: EventOfType<"goal.task_started">,
	tx: TransactionSql,
): Promise<void> {
	const q = asQueryable(tx);
	const { nodeId, taskId, turnId, runnerId } = event.data;

	const existing = await q<{ lastTurnId: string | null; status: TaskStatus }[]>`
		SELECT last_turn_id AS "lastTurnId", status FROM goal_task WHERE task_id = ${taskId}
	`;
	const row = existing[0];
	if (row === undefined) {
		// Task row doesn't exist — plan_updated handler is lagging. Throw so
		// the bus retries this event once the row exists.
		throw new GoalTaskOutOfOrderError(taskId);
	}
	// Terminal states are respected — a later task_started for an
	// already-completed or abandoned task is a no-op. This matters during
	// parallel replay where task_completed may have arrived first.
	if (row.status === "completed" || row.status === "abandoned" || row.status === "failed") {
		return;
	}
	// Idempotent renewal: same turn_id already in flight.
	if (row.lastTurnId === turnId && row.status === "in_progress") {
		return;
	}

	await q`
		UPDATE goal_task
		SET status = 'in_progress',
			last_turn_id = ${turnId},
			last_runner_id = ${runnerId},
			started_at = COALESCE(started_at, ${event.timestamp})
		WHERE task_id = ${taskId}
		  AND status NOT IN ('completed','abandoned','failed')
	`;

	await q`
		UPDATE goal_state
		SET current_task_id = ${taskId},
			last_worked_at = ${event.timestamp}
		WHERE node_id = ${nodeId}
		  AND current_task_id IS DISTINCT FROM ${taskId}
	`;
}

async function applyTaskCompleted(
	event: EventOfType<"goal.task_completed">,
	tx: TransactionSql,
): Promise<void> {
	const q = asQueryable(tx);
	const { nodeId, taskId, turnId } = event.data;

	const fresh = await applyIfFresh(taskId, turnId, q);
	if (!fresh) return;

	const updated = await q<{ taskId: string }[]>`
		UPDATE goal_task
		SET status = 'completed',
			completed_at = ${event.timestamp}
		WHERE task_id = ${taskId}
		RETURNING task_id AS "taskId"
	`;
	if (updated.length === 0) {
		// Task row doesn't exist yet — plan_updated handler is lagging.
		// Throw so the bus retries (up to MAX_RETRIES with backoff).
		throw new GoalTaskOutOfOrderError(taskId);
	}

	await q`
		UPDATE goal_state
		SET current_task_id = CASE WHEN current_task_id = ${taskId} THEN NULL ELSE current_task_id END,
			consecutive_failures = 0,
			last_worked_at = ${event.timestamp}
		WHERE node_id = ${nodeId}
	`;
}

async function applyTaskFailed(
	event: EventOfType<"goal.task_failed">,
	tx: TransactionSql,
): Promise<void> {
	const q = asQueryable(tx);
	const { nodeId, taskId, turnId } = event.data;

	const fresh = await applyIfFresh(taskId, turnId, q);
	if (!fresh) return;

	const updated = await q<{ taskId: string }[]>`
		UPDATE goal_task
		SET status = 'failed',
			completed_at = ${event.timestamp},
			failure_count = failure_count + 1
		WHERE task_id = ${taskId}
		RETURNING task_id AS "taskId"
	`;
	if (updated.length === 0) {
		throw new GoalTaskOutOfOrderError(taskId);
	}

	await q`
		UPDATE goal_state
		SET current_task_id = CASE WHEN current_task_id = ${taskId} THEN NULL ELSE current_task_id END,
			consecutive_failures = consecutive_failures + 1
		WHERE node_id = ${nodeId}
	`;
}

async function applyTaskYielded(
	event: EventOfType<"goal.task_yielded">,
	tx: TransactionSql,
): Promise<void> {
	const q = asQueryable(tx);
	const { nodeId, taskId, turnId, resumeKey } = event.data;

	const fresh = await applyIfFresh(taskId, turnId, q);
	if (!fresh) return;

	const updated = await q<{ taskId: string }[]>`
		UPDATE goal_task
		SET status = 'yielded',
			yield_count = yield_count + 1,
			resume_key = ${resumeKey}
		WHERE task_id = ${taskId}
		RETURNING task_id AS "taskId"
	`;
	if (updated.length === 0) {
		throw new GoalTaskOutOfOrderError(taskId);
	}

	await q`
		UPDATE goal_state
		SET current_task_id = CASE WHEN current_task_id = ${taskId} THEN NULL ELSE current_task_id END
		WHERE node_id = ${nodeId}
	`;
}

async function applyTaskAbandoned(
	event: EventOfType<"goal.task_abandoned">,
	tx: TransactionSql,
): Promise<void> {
	const q = asQueryable(tx);
	const { nodeId, taskId } = event.data;

	// Abandonment marks the task `abandoned` (terminal) but the NEXT plan
	// execution needs to retry from scratch. The executive calls the planner
	// when no ready task exists. Per foundation.md §7 and 12a-goal-loops.md
	// §7, the projection keeps abandonment terminal and the executive
	// re-runs planner on empty-ready.
	const updated = await q<{ taskId: string }[]>`
		UPDATE goal_task
		SET status = 'abandoned',
			completed_at = ${event.timestamp},
			failure_count = failure_count + 1
		WHERE task_id = ${taskId}
		  AND status <> 'abandoned'
		RETURNING task_id AS "taskId"
	`;
	// Tolerate missing rows for abandonment — if the task doesn't exist we
	// still want to bump the goal's failure counter (crash recovery can emit
	// abandonment for tasks the current plan no longer holds).
	void updated;

	await q`
		UPDATE goal_state
		SET current_task_id = CASE WHEN current_task_id = ${taskId} THEN NULL ELSE current_task_id END,
			consecutive_failures = consecutive_failures + 1
		WHERE node_id = ${nodeId}
	`;
}

async function applyBlocked(event: EventOfType<"goal.blocked">, tx: TransactionSql): Promise<void> {
	const q = asQueryable(tx);
	const blocker: BlockReason = event.data.blocker;
	await q`
		UPDATE goal_state
		SET status = 'blocked',
			blocked_reason = ${JSON.stringify(blocker)}
		WHERE node_id = ${event.data.nodeId}
		  AND status NOT IN ('completed','cancelled','expired','quarantined')
	`;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Gate task-state mutations by `turnId`. Returns true if this turn is
 * "fresh" — either the first event for the task, or a later turn than the
 * one we previously recorded. Returns false for stale/duplicate events so
 * the caller can no-op.
 *
 * Because turn IDs are ULIDs (lexicographically sortable), we compare
 * them as strings: the latest-stored ID wins.
 */
async function applyIfFresh(
	taskId: string,
	turnId: string,
	q: ReturnType<typeof asQueryable>,
): Promise<boolean> {
	const rows = await q<{ lastTurnId: string | null; status: string }[]>`
		SELECT last_turn_id AS "lastTurnId", status FROM goal_task WHERE task_id = ${taskId}
	`;
	const row = rows[0];
	if (row === undefined) return true; // no task yet — let the caller proceed (it will no-op if missing)
	const stored = row.lastTurnId;
	if (stored === null) return true;
	if (row.status === "completed" || row.status === "abandoned") return false;
	return turnId >= stored;
}

// ---------------------------------------------------------------------------
// Transition helper
// ---------------------------------------------------------------------------

interface TransitionOptions {
	readonly fromStatuses: readonly GoalStatus[];
}

async function updateStatus(
	nodeId: number,
	target: GoalStatus,
	tx: TransactionSql,
	opts: TransitionOptions,
): Promise<void> {
	const q = asQueryable(tx);
	const allowed = opts.fromStatuses as unknown as string[];
	await q`
		UPDATE goal_state
		SET status = ${target}
		WHERE node_id = ${nodeId}
		  AND status = ANY(${allowed}::text[])
	`;
}
