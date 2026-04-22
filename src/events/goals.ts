/**
 * Goal event catalog (Phase 12a).
 *
 * 21 discriminated variants covering the full BDI executive lifecycle:
 * creation, planning, leasing, task execution, blocking, reconsideration,
 * pause/resume/cancel, completion, quarantine, redaction, expiry.
 *
 * Every event is version 1. No upcasters — per foundation plan convention,
 * event schemas are modified directly during pre-production.
 *
 * The handler mode (`decision` vs `effect`) is NOT part of the payload —
 * it lives on the handler registration. Every event here is a pure data
 * record; the projection handler (decision) and the executive loop
 * (effect) consume them through the bus.
 *
 * Trust propagation: `effectiveTrust` on `goal.created` is computed at
 * emission time from the causation chain (foundation.md §7.3) and stored
 * on `goal_state`. Subagent dispatches carry the goal's `effectiveTrust`
 * via `metadata.goalEffectiveTrust` so downstream writes stay in tier.
 */

import type {
	BlockReason,
	GoalOrigin,
	GoalRunnerId,
	GoalTaskId,
	GoalTerminationReason,
	GoalTurnId,
	LeaseReleaseReason,
	PlanStep,
	ReconsiderationOutcome,
	ReconsiderationReason,
	TaskAbandonReason,
	TaskFailureClass,
	TaskYieldReason,
} from "../goals/types.ts";
import type { TrustTier } from "../memory/graph/types.ts";
import type { Actor, TheoEvent } from "./types.ts";

// ---------------------------------------------------------------------------
// Payload shapes
// ---------------------------------------------------------------------------

export interface GoalCreatedData {
	readonly nodeId: number;
	readonly title: string;
	readonly description: string;
	readonly origin: GoalOrigin;
	/** Owner-visible priority, 0-100. Default 50. */
	readonly ownerPriority: number;
	/** Trust tier walked from the causation chain at emission time. */
	readonly effectiveTrust: TrustTier;
	/** ISO timestamp for proposal expiry. Only set for proposed goals. */
	readonly proposalExpiresAt?: string | undefined;
}

export interface GoalConfirmedData {
	readonly nodeId: number;
	/** Must be `owner` or a user actor — enforced at the command boundary. */
	readonly confirmedBy: Actor;
}

export interface GoalPriorityChangedData {
	readonly nodeId: number;
	readonly oldPriority: number;
	readonly newPriority: number;
	readonly reason: string;
}

export interface GoalPlanUpdatedData {
	readonly nodeId: number;
	/** Monotonically increasing version of this goal's plan. */
	readonly planVersion: number;
	/** FULL snapshot of the plan. Never a diff. */
	readonly plan: readonly PlanStep[];
	readonly reason: string;
	/** SHA256 of the previous plan, or null for the first plan. Enables drift detection. */
	readonly previousPlanHash: string | null;
}

export interface GoalLeaseAcquiredData {
	readonly nodeId: number;
	readonly runnerId: GoalRunnerId;
	readonly leaseDurationMs: number;
}

export interface GoalLeaseReleasedData {
	readonly nodeId: number;
	readonly runnerId: GoalRunnerId;
	readonly reason: LeaseReleaseReason;
}

export interface GoalTaskStartedData {
	readonly nodeId: number;
	readonly taskId: GoalTaskId;
	readonly turnId: GoalTurnId;
	readonly runnerId: GoalRunnerId;
	readonly subagent: string;
	readonly maxTurns: number;
	readonly maxBudgetUsd: number;
	readonly maxDurationMs: number;
}

export interface GoalTaskProgressData {
	readonly nodeId: number;
	readonly taskId: GoalTaskId;
	readonly turnId: GoalTurnId;
	readonly note: string;
	readonly tokensConsumed: number;
	readonly costUsdConsumed: number;
}

export interface GoalTaskYieldedData {
	readonly nodeId: number;
	readonly taskId: GoalTaskId;
	readonly turnId: GoalTurnId;
	/** Points at a `resume_context.id` row when present. */
	readonly resumeKey: string | null;
	readonly reason: TaskYieldReason;
}

export interface GoalTaskCompletedData {
	readonly nodeId: number;
	readonly taskId: GoalTaskId;
	readonly turnId: GoalTurnId;
	readonly outcome: string;
	/** ULIDs of memory nodes, PRs, or other artifacts produced. */
	readonly artifactIds: readonly string[];
	readonly totalTokens: number;
	readonly totalCostUsd: number;
}

export interface GoalTaskFailedData {
	readonly nodeId: number;
	readonly taskId: GoalTaskId;
	readonly turnId: GoalTurnId;
	readonly errorClass: TaskFailureClass;
	readonly message: string;
	readonly recoverable: boolean;
}

export interface GoalTaskAbandonedData {
	readonly nodeId: number;
	readonly taskId: GoalTaskId;
	readonly previousTurnId: GoalTurnId;
	readonly previousRunnerId: GoalRunnerId;
	readonly reason: TaskAbandonReason;
}

export interface GoalBlockedData {
	readonly nodeId: number;
	readonly taskId: GoalTaskId;
	readonly blocker: BlockReason;
}

export interface GoalUnblockedData {
	readonly nodeId: number;
	readonly taskId: GoalTaskId;
	readonly unblockedBy: Actor;
}

export interface GoalReconsideredData {
	readonly nodeId: number;
	readonly reason: ReconsiderationReason;
	readonly outcome: ReconsiderationOutcome;
}

export interface GoalPausedData {
	readonly nodeId: number;
	readonly pausedBy: Actor;
}

export interface GoalResumedData {
	readonly nodeId: number;
	readonly resumedBy: Actor;
}

export interface GoalCancelledData {
	readonly nodeId: number;
	readonly cancelledBy: Actor;
	readonly reason: GoalTerminationReason;
}

export interface GoalCompletedData {
	readonly nodeId: number;
	readonly finalOutcome: string;
	readonly totalTurns: number;
	readonly totalCostUsd: number;
}

export interface GoalQuarantinedData {
	readonly nodeId: number;
	readonly consecutiveFailures: number;
	readonly reason: string;
}

export type GoalRedactedField = "title" | "description" | "plan_bodies" | "task_bodies";

export interface GoalRedactedData {
	readonly nodeId: number;
	readonly redactedFields: readonly GoalRedactedField[];
	readonly redactedBy: Actor;
}

export interface GoalExpiredData {
	readonly nodeId: number;
}

// ---------------------------------------------------------------------------
// Full union — added to the top-level Event union in events/types.ts
// ---------------------------------------------------------------------------

export type GoalEvent =
	| TheoEvent<"goal.created", GoalCreatedData>
	| TheoEvent<"goal.confirmed", GoalConfirmedData>
	| TheoEvent<"goal.priority_changed", GoalPriorityChangedData>
	| TheoEvent<"goal.plan_updated", GoalPlanUpdatedData>
	| TheoEvent<"goal.lease_acquired", GoalLeaseAcquiredData>
	| TheoEvent<"goal.lease_released", GoalLeaseReleasedData>
	| TheoEvent<"goal.task_started", GoalTaskStartedData>
	| TheoEvent<"goal.task_progress", GoalTaskProgressData>
	| TheoEvent<"goal.task_yielded", GoalTaskYieldedData>
	| TheoEvent<"goal.task_completed", GoalTaskCompletedData>
	| TheoEvent<"goal.task_failed", GoalTaskFailedData>
	| TheoEvent<"goal.task_abandoned", GoalTaskAbandonedData>
	| TheoEvent<"goal.blocked", GoalBlockedData>
	| TheoEvent<"goal.unblocked", GoalUnblockedData>
	| TheoEvent<"goal.reconsidered", GoalReconsideredData>
	| TheoEvent<"goal.paused", GoalPausedData>
	| TheoEvent<"goal.resumed", GoalResumedData>
	| TheoEvent<"goal.cancelled", GoalCancelledData>
	| TheoEvent<"goal.completed", GoalCompletedData>
	| TheoEvent<"goal.quarantined", GoalQuarantinedData>
	| TheoEvent<"goal.redacted", GoalRedactedData>
	| TheoEvent<"goal.expired", GoalExpiredData>;
