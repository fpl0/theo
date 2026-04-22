/**
 * Goal loop type surface.
 *
 * Every branded ID is a ULID at runtime. GoalTaskId and GoalTurnId and
 * GoalRunnerId share that shape but live in the type system as distinct
 * brands so a turn ID can never be passed where a task ID is expected.
 *
 * `GoalNodeId` is a bridge to the knowledge graph's `NodeId` — a goal IS a
 * `NodeKind='goal'` node, so its primary key is the node's integer id.
 */

import { monotonicFactory } from "ulid";
import type { Actor } from "../events/types.ts";
import type { NodeId, TrustTier } from "../memory/graph/types.ts";
import type { JsonValue } from "../memory/types.ts";

const ulid = monotonicFactory();

// ---------------------------------------------------------------------------
// Branded IDs
// ---------------------------------------------------------------------------

export type GoalTaskId = string & { readonly __brand: "GoalTaskId" };
export type GoalTurnId = string & { readonly __brand: "GoalTurnId" };
export type GoalRunnerId = string & { readonly __brand: "GoalRunnerId" };
export type ResumeContextId = string & { readonly __brand: "ResumeContextId" };

export function newGoalTaskId(): GoalTaskId {
	return ulid() as GoalTaskId;
}

export function newGoalTurnId(): GoalTurnId {
	return ulid() as GoalTurnId;
}

export function newGoalRunnerId(): GoalRunnerId {
	return ulid() as GoalRunnerId;
}

export function newResumeContextId(): ResumeContextId {
	return ulid() as ResumeContextId;
}

/** Narrow a raw string to GoalTaskId. The `as` cast is the one allowed exception. */
export function asGoalTaskId(s: string): GoalTaskId {
	return s as GoalTaskId;
}

/** Narrow a raw string to GoalTurnId. */
export function asGoalTurnId(s: string): GoalTurnId {
	return s as GoalTurnId;
}

/** Narrow a raw string to GoalRunnerId. */
export function asGoalRunnerId(s: string): GoalRunnerId {
	return s as GoalRunnerId;
}

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

export type GoalOrigin = "owner" | "ideation" | "reflex" | "system";

export type GoalStatus =
	| "proposed"
	| "active"
	| "blocked"
	| "paused"
	| "completed"
	| "cancelled"
	| "quarantined"
	| "expired";

export type TaskStatus =
	| "pending"
	| "in_progress"
	| "yielded"
	| "completed"
	| "failed"
	| "abandoned";

export type BlockReason =
	| { readonly kind: "user_input"; readonly question: string }
	| { readonly kind: "resource"; readonly resource: string }
	| { readonly kind: "external"; readonly service: string }
	| { readonly kind: "budget"; readonly cap: "turn" | "goal" | "daily" }
	| { readonly kind: "degradation"; readonly level: number };

export type ReconsiderationReason =
	| "higher_priority_arrived"
	| "contradiction_detected"
	| "budget_exhausted"
	| "owner_command"
	| "periodic_review";

export type ReconsiderationOutcome =
	| "stay_committed"
	| "plan_updated"
	| "goal_yielded"
	| "goal_abandoned";

export type GoalTerminationReason =
	| "objective_met"
	| "no_longer_relevant"
	| "owner_cancelled"
	| "poison_quarantine"
	| "proposal_expired"
	| "superseded_by";

export type TaskFailureClass =
	| "tool_error"
	| "llm_error"
	| "validation_error"
	| "timeout"
	| "abort"
	| "internal";

export type TaskYieldReason = "preempted" | "turn_budget_exceeded" | "waiting_for_result";

export type TaskAbandonReason = "crash_recovery" | "lease_expired" | "force_abort";

export type LeaseReleaseReason = "normal" | "expiry" | "abandonment";

// ---------------------------------------------------------------------------
// Plan step
// ---------------------------------------------------------------------------

/**
 * A single step in a goal's plan. Carried on `goal.plan_updated` as a full
 * snapshot — the projection writes one `goal_task` row per step.
 */
export interface PlanStep {
	readonly taskId: GoalTaskId;
	readonly body: string;
	readonly dependsOn: readonly GoalTaskId[];
	readonly preferredSubagent?: string | undefined;
}

// ---------------------------------------------------------------------------
// Persisted projection records
// ---------------------------------------------------------------------------

/**
 * Execution state of an active goal. This is the row in `goal_state`
 * projected from the `goal.*` events. The underlying node (kind=goal)
 * carries title/description/metadata; this record carries the hot-path
 * runtime state.
 */
export interface GoalState {
	readonly nodeId: NodeId;
	readonly status: GoalStatus;
	readonly origin: GoalOrigin;
	readonly ownerPriority: number;
	readonly effectiveTrust: TrustTier;
	readonly planVersion: number;
	readonly plan: readonly PlanStep[];
	readonly currentTaskId: GoalTaskId | null;
	readonly consecutiveFailures: number;
	readonly leasedBy: GoalRunnerId | null;
	readonly leasedUntil: Date | null;
	readonly blockedReason: BlockReason | null;
	readonly quarantinedReason: string | null;
	readonly lastReconsideredAt: Date | null;
	readonly lastWorkedAt: Date | null;
	readonly proposedExpiresAt: Date | null;
	readonly redacted: boolean;
	readonly createdAt: Date;
	readonly updatedAt: Date;
}

/**
 * Execution state of a single task within a goal's plan. Rows are inserted
 * by the projection on `goal.plan_updated` (one per PlanStep) and mutated
 * by subsequent task events.
 */
export interface GoalTask {
	readonly taskId: GoalTaskId;
	readonly goalNodeId: NodeId;
	readonly planVersion: number;
	readonly stepOrder: number;
	readonly body: string;
	readonly dependsOn: readonly GoalTaskId[];
	readonly status: TaskStatus;
	readonly subagent: string | null;
	readonly startedAt: Date | null;
	readonly completedAt: Date | null;
	readonly lastTurnId: GoalTurnId | null;
	readonly lastRunnerId: GoalRunnerId | null;
	readonly failureCount: number;
	readonly yieldCount: number;
	readonly resumeKey: ResumeContextId | null;
	readonly redacted: boolean;
	readonly createdAt: Date;
}

/**
 * Autonomy policy row. Each domain has a level 0-5; `set_by` records which
 * actor set the current level (`system` for seeded defaults).
 */
export interface AutonomyPolicy {
	readonly domain: string;
	readonly level: number;
	readonly setBy: Actor;
	readonly setAt: Date;
	readonly reason: string | null;
}

// ---------------------------------------------------------------------------
// Goal node metadata
// ---------------------------------------------------------------------------

/**
 * Structured attributes written to the underlying `NodeKind='goal'` node's
 * `metadata` JSONB column at creation time.
 *
 * `body` on the node remains the authoritative embeddable text; this
 * structure is advisory routing/provenance info. The executive reads it via
 * the repository layer so the goal node stays self-describing when pulled up
 * by RRF retrieval.
 */
export interface GoalNodeMetadata {
	readonly [key: string]: JsonValue;
	readonly title: string;
	readonly description: string;
	readonly origin: GoalOrigin;
	readonly ownerPriority: number;
}

// ---------------------------------------------------------------------------
// Command errors
// ---------------------------------------------------------------------------

/** Discriminated error union for operator command handlers. */
export type CommandError =
	| { readonly code: "not_found" }
	| { readonly code: "invalid_state"; readonly status: GoalStatus }
	| { readonly code: "forbidden"; readonly reason: string }
	| { readonly code: "invalid_argument"; readonly reason: string };

// ---------------------------------------------------------------------------
// Tunables
// ---------------------------------------------------------------------------

/** Default lease duration in ms (5 minutes). */
export const DEFAULT_LEASE_DURATION_MS = 5 * 60_000;

/** Consecutive task failures that trigger goal quarantine. */
export const POISON_THRESHOLD = 3;

/** Priority aging bonus in points per week of staleness. */
export const AGING_WEEKLY_BONUS = 10;

/** Maximum plan_version allowed per goal (prevents planner loops). */
export const MAX_PLAN_VERSION = 10;

/** Resume context TTL in ms (24 hours). */
export const RESUME_CONTEXT_TTL_MS = 24 * 60 * 60_000;

/** Ideation-origin goals are hard-capped at this autonomy level. */
export const IDEATION_AUTONOMY_CAP = 2;
