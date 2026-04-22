/**
 * Intention reconsideration policy (foundation.md §7.1).
 *
 * Single-minded commitment by default — once the executive has committed to
 * a task, it runs the task to completion without second-guessing. Four
 * triggers can force reconsideration:
 *
 *   1. `higher_priority_arrived` — a higher-priority class (interactive) is
 *      waiting. The executive yields.
 *   2. `contradiction_detected` — a fresh `memory.contradiction.detected`
 *      event names a node that appears in the current goal's plan. The
 *      plan may be stale; the planner re-plans.
 *   3. `budget_exhausted` — per-goal cumulative cost is within 10% of the
 *      cap. The executive yields before burning more budget.
 *   4. `owner_command` — `/pause` or `/priority` changed mid-turn. The
 *      executive honors the command on the next tick.
 *   5. `periodic_review` — N turns since the last reconsideration. Forces
 *      a quick sanity check even when no other trigger fires.
 *
 * The reconsideration check is a decision function — pure over the state
 * it's given. The executive calls it between steps, NOT inside a subagent
 * turn; a subagent runs to its own completion once dispatched.
 */

import { budgetRemaining } from "./stopping.ts";
import type {
	BlockReason,
	GoalState,
	GoalTask,
	ReconsiderationOutcome,
	ReconsiderationReason,
} from "./types.ts";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Number of completed turns between forced periodic reviews. */
export const PERIODIC_REVIEW_INTERVAL = 10;

/** Budget headroom fraction below which `budget_exhausted` triggers. */
export const BUDGET_HEADROOM_FRACTION = 0.1;

// ---------------------------------------------------------------------------
// Inputs and outputs
// ---------------------------------------------------------------------------

export interface ReconsiderationInput {
	readonly goal: GoalState;
	readonly task: GoalTask;
	/** True when an interactive or higher-class item is waiting. */
	readonly higherPriorityWaiting: boolean;
	/** Node ids that have a recent contradiction. */
	readonly contradictingNodeIds: readonly number[];
	/** Cumulative USD spent on this goal so far. */
	readonly totalCostUsd: number;
	/** Per-goal cumulative cap. */
	readonly goalBudgetUsd: number;
	/** Number of task turns since the last reconsideration. */
	readonly turnsSinceLastReview: number;
	/** True if the goal has a pending owner command (pause/priority). */
	readonly pendingOwnerCommand: boolean;
	/** True if the goal is currently blocked (should always yield). */
	readonly externalBlocker: BlockReason | null;
}

export interface ReconsiderationDecision {
	readonly reconsider: boolean;
	readonly reason: ReconsiderationReason;
	readonly outcome: ReconsiderationOutcome;
}

const STAY: ReconsiderationDecision = {
	reconsider: false,
	reason: "periodic_review",
	outcome: "stay_committed",
};

// ---------------------------------------------------------------------------
// Decision
// ---------------------------------------------------------------------------

/**
 * Pure function: should the executive reconsider its current intention?
 *
 * The order of trigger checks matters. External blockers first — nothing
 * else makes sense if the goal can't proceed. Then owner commands (highest
 * operator authority). Then preemption from higher-priority turns. Then
 * budget (economic). Then contradictions (epistemic). Periodic review last.
 */
export function shouldReconsider(input: ReconsiderationInput): ReconsiderationDecision {
	if (input.externalBlocker !== null) {
		return {
			reconsider: true,
			reason: "budget_exhausted", // blocker is emitted separately; here we signal "don't start"
			outcome: "goal_yielded",
		};
	}

	if (input.pendingOwnerCommand) {
		return {
			reconsider: true,
			reason: "owner_command",
			outcome: "goal_yielded",
		};
	}

	if (input.higherPriorityWaiting) {
		return {
			reconsider: true,
			reason: "higher_priority_arrived",
			outcome: "goal_yielded",
		};
	}

	const remaining = budgetRemaining(input.totalCostUsd, input.goalBudgetUsd);
	const budgetFraction = input.goalBudgetUsd === 0 ? 0 : remaining / input.goalBudgetUsd;
	if (budgetFraction <= BUDGET_HEADROOM_FRACTION) {
		return {
			reconsider: true,
			reason: "budget_exhausted",
			outcome: "goal_yielded",
		};
	}

	if (input.contradictingNodeIds.length > 0) {
		return {
			reconsider: true,
			reason: "contradiction_detected",
			outcome: "plan_updated",
		};
	}

	if (input.turnsSinceLastReview >= PERIODIC_REVIEW_INTERVAL) {
		return {
			reconsider: true,
			reason: "periodic_review",
			outcome: "stay_committed",
		};
	}

	return STAY;
}
