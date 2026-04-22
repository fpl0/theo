/**
 * Goal handler registration — glues the projection, poison breaker, and
 * reconsideration-contradiction hook together.
 *
 * Projection handlers live in `projection.ts`; the executive loop lives in
 * `executive.ts` and is registered by the caller (scheduler priority-class
 * `executive`). This module wires everything else:
 *
 *   1. Register the projection (22 decision handlers).
 *   2. Register the poison-goal circuit breaker — triggers
 *      `goal.quarantined` + a `notification.created` after
 *      `POISON_THRESHOLD` consecutive failures.
 *
 * Keeping this module thin makes the startup sequence explicit: callers
 * call `registerGoalHandlers(deps)` once during bus bootstrap.
 */

import { asNodeId } from "../memory/graph/types.ts";
import { type ProjectionDeps, registerGoalProjection } from "./projection.ts";
import type { GoalRepository } from "./repository.ts";
import { POISON_THRESHOLD } from "./types.ts";

// ---------------------------------------------------------------------------
// Dependencies
// ---------------------------------------------------------------------------

export interface GoalHandlerDeps extends ProjectionDeps {
	readonly goals: GoalRepository;
}

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------

export function registerGoalHandlers(deps: GoalHandlerDeps): void {
	registerGoalProjection(deps);
	registerPoisonBreaker(deps);
}

/**
 * Poison quarantine: when a `goal.task_failed` / `goal.task_abandoned`
 * event pushes consecutive_failures past the threshold, emit
 * `goal.quarantined` and a user-visible `notification.created`.
 *
 * The handler is `decision` — deterministic read of the projection plus
 * event emissions. Replay-safe because the quarantine event itself
 * carries the count, and the projection's quarantine rule is idempotent
 * on repeated application.
 *
 * IMPORTANT: the poison breaker MUST NOT call `bus.flush()` inside its
 * handler — that would deadlock (the handler runs inside the drain
 * loop; flush waits for every queue to quiesce). Instead we retry the
 * projection read a small number of times with a tight backoff, because
 * the projection handler's transaction commits before this handler
 * fires but the bus dispatches handlers in parallel across queues.
 */
function registerPoisonBreaker(deps: GoalHandlerDeps): void {
	const { bus, goals } = deps;

	bus.on(
		"goal.task_failed",
		async (event) => {
			await handlePossibleQuarantine(
				bus,
				goals,
				event.id,
				event.data.nodeId,
				`${String(event.data.taskId)}: ${event.data.message}`,
				"failure",
			);
		},
		{ id: "goal-poison-breaker", mode: "decision" },
	);

	bus.on(
		"goal.task_abandoned",
		async (event) => {
			await handlePossibleQuarantine(
				bus,
				goals,
				event.id,
				event.data.nodeId,
				`task ${String(event.data.taskId)} abandoned`,
				"abandonment",
			);
		},
		{ id: "goal-poison-breaker-abandoned", mode: "decision" },
	);
}

const POISON_READ_ATTEMPTS = 8;
const POISON_READ_DELAY_MS = 15;

async function handlePossibleQuarantine(
	bus: import("../events/bus.ts").EventBus,
	goals: GoalRepository,
	causeId: import("../events/ids.ts").EventId,
	nodeId: number,
	eventSummary: string,
	kind: "failure" | "abandonment",
): Promise<void> {
	// Retry loop: the sibling projection handler may not have committed
	// its consecutive_failures bump yet. Give it a few ticks to land.
	let state: Awaited<ReturnType<GoalRepository["readState"]>> = null;
	for (let attempt = 0; attempt < POISON_READ_ATTEMPTS; attempt++) {
		state = await goals.readState(asNodeId(nodeId));
		if (state === null) break;
		if (state.consecutiveFailures >= POISON_THRESHOLD || state.status === "quarantined") break;
		await new Promise<void>((resolve) => setTimeout(resolve, POISON_READ_DELAY_MS));
	}
	if (state === null) return;
	if (state.consecutiveFailures < POISON_THRESHOLD) return;
	if (state.status === "quarantined") return;

	await bus.emit({
		type: "goal.quarantined",
		version: 1,
		actor: "system",
		data: {
			nodeId,
			consecutiveFailures: state.consecutiveFailures,
			reason:
				kind === "failure"
					? `${String(state.consecutiveFailures)} consecutive failures in ${eventSummary}`
					: `${String(state.consecutiveFailures)} consecutive ${eventSummary}`,
		},
		metadata: { causeId },
	});
	await bus.emit({
		type: "notification.created",
		version: 1,
		actor: "system",
		data: {
			source: "goal-quarantine",
			body:
				`Goal #${String(nodeId)} quarantined after ` +
				`${String(state.consecutiveFailures)} ${kind === "failure" ? "failures" : "abandonments"}. ` +
				`/audit ${String(nodeId)} for details.`,
		},
		metadata: { causeId },
	});
}
