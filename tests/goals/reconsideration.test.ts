/**
 * Reconsideration policy — pure function unit tests.
 */

import { describe, expect, test } from "bun:test";
import { PERIODIC_REVIEW_INTERVAL, shouldReconsider } from "../../src/goals/reconsideration.ts";
import type { BlockReason, GoalState, GoalTask } from "../../src/goals/types.ts";
import { asGoalRunnerId, asGoalTaskId, asGoalTurnId } from "../../src/goals/types.ts";
import { asNodeId } from "../../src/memory/graph/types.ts";

function makeGoal(overrides: Partial<GoalState> = {}): GoalState {
	return {
		nodeId: asNodeId(1),
		status: "active",
		origin: "owner",
		ownerPriority: 50,
		effectiveTrust: "owner",
		planVersion: 1,
		plan: [],
		currentTaskId: asGoalTaskId("t1"),
		consecutiveFailures: 0,
		leasedBy: asGoalRunnerId("r1"),
		leasedUntil: new Date(Date.now() + 60_000),
		blockedReason: null,
		quarantinedReason: null,
		lastReconsideredAt: null,
		lastWorkedAt: null,
		proposedExpiresAt: null,
		redacted: false,
		createdAt: new Date(),
		updatedAt: new Date(),
		...overrides,
	};
}

function makeTask(overrides: Partial<GoalTask> = {}): GoalTask {
	return {
		taskId: asGoalTaskId("t1"),
		goalNodeId: asNodeId(1),
		planVersion: 1,
		stepOrder: 0,
		body: "do it",
		dependsOn: [],
		status: "in_progress",
		subagent: "planner",
		startedAt: new Date(),
		completedAt: null,
		lastTurnId: asGoalTurnId("turn1"),
		lastRunnerId: asGoalRunnerId("r1"),
		failureCount: 0,
		yieldCount: 0,
		resumeKey: null,
		redacted: false,
		createdAt: new Date(),
		...overrides,
	};
}

describe("shouldReconsider", () => {
	test("stays committed by default", () => {
		const decision = shouldReconsider({
			goal: makeGoal(),
			task: makeTask(),
			higherPriorityWaiting: false,
			contradictingNodeIds: [],
			totalCostUsd: 0,
			goalBudgetUsd: 1.0,
			turnsSinceLastReview: 0,
			pendingOwnerCommand: false,
			externalBlocker: null,
		});
		expect(decision.reconsider).toBe(false);
	});

	test("external blocker forces yield", () => {
		const blocker: BlockReason = { kind: "external", service: "gmail" };
		const decision = shouldReconsider({
			goal: makeGoal(),
			task: makeTask(),
			higherPriorityWaiting: false,
			contradictingNodeIds: [],
			totalCostUsd: 0,
			goalBudgetUsd: 1.0,
			turnsSinceLastReview: 0,
			pendingOwnerCommand: false,
			externalBlocker: blocker,
		});
		expect(decision.reconsider).toBe(true);
		expect(decision.outcome).toBe("goal_yielded");
	});

	test("pending owner command yields", () => {
		const decision = shouldReconsider({
			goal: makeGoal(),
			task: makeTask(),
			higherPriorityWaiting: false,
			contradictingNodeIds: [],
			totalCostUsd: 0,
			goalBudgetUsd: 1.0,
			turnsSinceLastReview: 0,
			pendingOwnerCommand: true,
			externalBlocker: null,
		});
		expect(decision.reconsider).toBe(true);
		expect(decision.reason).toBe("owner_command");
	});

	test("higher priority waiting yields", () => {
		const decision = shouldReconsider({
			goal: makeGoal(),
			task: makeTask(),
			higherPriorityWaiting: true,
			contradictingNodeIds: [],
			totalCostUsd: 0,
			goalBudgetUsd: 1.0,
			turnsSinceLastReview: 0,
			pendingOwnerCommand: false,
			externalBlocker: null,
		});
		expect(decision.reason).toBe("higher_priority_arrived");
	});

	test("budget near cap triggers budget_exhausted", () => {
		const decision = shouldReconsider({
			goal: makeGoal(),
			task: makeTask(),
			higherPriorityWaiting: false,
			contradictingNodeIds: [],
			totalCostUsd: 0.95,
			goalBudgetUsd: 1.0, // 5% remaining, below 10% headroom
			turnsSinceLastReview: 0,
			pendingOwnerCommand: false,
			externalBlocker: null,
		});
		expect(decision.reason).toBe("budget_exhausted");
	});

	test("contradiction triggers plan_updated", () => {
		const decision = shouldReconsider({
			goal: makeGoal(),
			task: makeTask(),
			higherPriorityWaiting: false,
			contradictingNodeIds: [42],
			totalCostUsd: 0,
			goalBudgetUsd: 1.0,
			turnsSinceLastReview: 0,
			pendingOwnerCommand: false,
			externalBlocker: null,
		});
		expect(decision.reason).toBe("contradiction_detected");
		expect(decision.outcome).toBe("plan_updated");
	});

	test("periodic review at interval threshold", () => {
		const decision = shouldReconsider({
			goal: makeGoal(),
			task: makeTask(),
			higherPriorityWaiting: false,
			contradictingNodeIds: [],
			totalCostUsd: 0,
			goalBudgetUsd: 1.0,
			turnsSinceLastReview: PERIODIC_REVIEW_INTERVAL,
			pendingOwnerCommand: false,
			externalBlocker: null,
		});
		expect(decision.reason).toBe("periodic_review");
		expect(decision.outcome).toBe("stay_committed");
	});

	test("owner command outranks budget exhaustion", () => {
		const decision = shouldReconsider({
			goal: makeGoal(),
			task: makeTask(),
			higherPriorityWaiting: false,
			contradictingNodeIds: [],
			totalCostUsd: 0.99,
			goalBudgetUsd: 1.0,
			turnsSinceLastReview: 0,
			pendingOwnerCommand: true,
			externalBlocker: null,
		});
		expect(decision.reason).toBe("owner_command");
	});
});
