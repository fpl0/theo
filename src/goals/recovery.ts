/**
 * Recovery — synthesize `goal.task_abandoned` events for tasks that were
 * in-progress when the last executive process died.
 *
 * A task row with `status = 'in_progress'` whose `last_runner_id` does not
 * match the current process runner id is dangling: the original subagent is
 * gone, and re-running the task is cheaper than trying to reconstruct
 * partial state. The recovery job emits one `goal.task_abandoned` per
 * dangling task, which:
 *
 *   1. Marks the task `abandoned` in the projection.
 *   2. Clears `current_task_id` on the goal.
 *   3. Increments `consecutive_failures` — repeated abandonment counts
 *      toward the poison quarantine circuit breaker.
 *
 * Also releases stale leases (leased_until < now) so the next executive
 * tick can re-acquire them.
 */

import type { EventBus } from "../events/bus.ts";
import type { GoalRepository } from "./repository.ts";
import { asGoalRunnerId, type GoalRunnerId, newGoalTurnId } from "./types.ts";

// ---------------------------------------------------------------------------
// Dependencies
// ---------------------------------------------------------------------------

export interface RecoveryDeps {
	readonly bus: EventBus;
	readonly goals: GoalRepository;
	readonly now?: () => Date;
}

/** Result of a recovery sweep — counts for observability. */
export interface RecoveryResult {
	readonly abandonedTasks: number;
	readonly releasedLeases: number;
}

// ---------------------------------------------------------------------------
// Recovery
// ---------------------------------------------------------------------------

/**
 * Run one recovery sweep. Call at startup before the executive loop begins
 * accepting ticks.
 *
 * `currentRunnerId` is the new process's runner id. Any in-progress task
 * whose `last_runner_id` matches the current one is left alone — that
 * means this process is the one that started it (e.g., when recovery
 * runs twice in a single process lifetime, which shouldn't happen but
 * should be idempotent).
 */
export async function runRecovery(
	deps: RecoveryDeps,
	currentRunnerId: GoalRunnerId,
): Promise<RecoveryResult> {
	const { bus, goals } = deps;
	const now = (deps.now ?? ((): Date => new Date()))();
	let abandonedTasks = 0;
	let releasedLeases = 0;

	// ---------------------------------------------------------------------------
	// Step 1: abandon dangling in-progress tasks
	// ---------------------------------------------------------------------------

	const dangling = await goals.inProgressTasks();
	for (const task of dangling) {
		if (task.lastRunnerId === currentRunnerId) continue;
		// No pre-crash turn id survived — mint a synthetic one so downstream
		// audit can tell crash-recovery events apart from ordinary abandons.
		const previousTurnId = task.lastTurnId ?? newGoalTurnId();
		const previousRunnerId = task.lastRunnerId ?? asGoalRunnerId("unknown");
		await bus.emit({
			type: "goal.task_abandoned",
			version: 1,
			actor: "system",
			data: {
				nodeId: Number(task.goalNodeId),
				taskId: task.taskId,
				previousTurnId,
				previousRunnerId,
				reason: "crash_recovery",
			},
			metadata: {},
		});
		abandonedTasks++;
	}

	// ---------------------------------------------------------------------------
	// Step 2: release stale leases
	// ---------------------------------------------------------------------------

	const leased = await goals.leasedGoals();
	for (const g of leased) {
		if (g.leasedBy === null) continue;
		if (g.leasedUntil !== null && g.leasedUntil > now) continue;
		await bus.emit({
			type: "goal.lease_released",
			version: 1,
			actor: "system",
			data: {
				nodeId: Number(g.nodeId),
				runnerId: g.leasedBy,
				reason: "expiry",
			},
			metadata: {},
		});
		releasedLeases++;
	}

	await bus.flush();
	return { abandonedTasks, releasedLeases };
}
