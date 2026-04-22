/**
 * Operator commands for the goal executive.
 *
 * Every command translates a gate event (CLI slash command, Telegram
 * command) into a durable `goal.*` event. Gates enforce the actor tier:
 * CLI commands arrive as `actor: "user"` with implicit `owner` trust,
 * Telegram commands arrive as `actor: "user"` with `verified` trust, and
 * CLI-only commands (`/redact`, `/autonomy`) reject other gates here.
 *
 * Commands never read uncommitted state; they emit events and let the
 * projection decision handler reconcile.
 */

import { err, ok, type Result } from "../errors.ts";
import type { EventBus } from "../events/bus.ts";
import type { GoalRedactedField } from "../events/goals.ts";
import type { Actor } from "../events/types.ts";
import type { NodeId, TrustTier } from "../memory/graph/types.ts";
import type { AuditEventRow, GoalRepository } from "./repository.ts";
import type { CommandError, GoalTerminationReason } from "./types.ts";

export type { AuditEventRow } from "./repository.ts";
// Re-export for consumer ergonomics.
export type { CommandError } from "./types.ts";

// ---------------------------------------------------------------------------
// Dependencies
// ---------------------------------------------------------------------------

export interface CommandDeps {
	readonly bus: EventBus;
	readonly goals: GoalRepository;
}

/** Trust tier that the gate grants by default; CLI=owner, Telegram=verified. */
export type CommandChannel = "cli" | "telegram" | "system";

// Internal system commands run at owner trust — they are triggered by the
// engine itself (recovery, scheduler housekeeping), never by a remote gate.
const CHANNEL_TRUST: Record<CommandChannel, TrustTier> = {
	cli: "owner",
	telegram: "verified",
	system: "owner",
};

/** CLI-only commands — any non-cli caller is rejected with `forbidden`. */
const CLI_ONLY_COMMANDS = new Set<string>(["/redact", "/autonomy"]);

/** Commands that require the CLI channel. */
export function isCliOnlyCommand(name: string): boolean {
	return CLI_ONLY_COMMANDS.has(name);
}

export function commandTrust(channel: CommandChannel): TrustTier {
	return CHANNEL_TRUST[channel];
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

/** Pause a goal — excludes it from lease acquisition. */
export async function pauseGoal(
	deps: CommandDeps,
	nodeId: NodeId,
	actor: Actor,
): Promise<Result<void, CommandError>> {
	const state = await deps.goals.readState(nodeId);
	if (state === null) return err({ code: "not_found" });
	if (state.status === "paused") return ok(undefined); // idempotent
	if (state.status === "completed" || state.status === "cancelled" || state.status === "expired") {
		return err({ code: "invalid_state", status: state.status });
	}
	await deps.bus.emit({
		type: "goal.paused",
		version: 1,
		actor,
		data: { nodeId: Number(nodeId), pausedBy: actor },
		metadata: {},
	});
	return ok(undefined);
}

/** Resume a paused goal. Also resets `consecutive_failures` when called on quarantined. */
export async function resumeGoal(
	deps: CommandDeps,
	nodeId: NodeId,
	actor: Actor,
): Promise<Result<void, CommandError>> {
	const state = await deps.goals.readState(nodeId);
	if (state === null) return err({ code: "not_found" });
	if (state.status === "active") return ok(undefined); // idempotent
	if (state.status !== "paused" && state.status !== "quarantined") {
		return err({ code: "invalid_state", status: state.status });
	}
	// For quarantined goals, operator-driven resume is implemented as a
	// priority reset event — projection handler clears consecutive_failures
	// on priority_changed.
	if (state.status === "quarantined") {
		await deps.bus.emit({
			type: "goal.priority_changed",
			version: 1,
			actor,
			data: {
				nodeId: Number(nodeId),
				oldPriority: state.ownerPriority,
				newPriority: state.ownerPriority,
				reason: "operator-resume from quarantine",
			},
			metadata: {},
		});
	}
	await deps.bus.emit({
		type: "goal.resumed",
		version: 1,
		actor,
		data: { nodeId: Number(nodeId), resumedBy: actor },
		metadata: {},
	});
	return ok(undefined);
}

/** Cancel a goal — terminal state. */
export async function cancelGoal(
	deps: CommandDeps,
	nodeId: NodeId,
	actor: Actor,
	reason: GoalTerminationReason = "owner_cancelled",
): Promise<Result<void, CommandError>> {
	const state = await deps.goals.readState(nodeId);
	if (state === null) return err({ code: "not_found" });
	if (state.status === "cancelled" || state.status === "completed" || state.status === "expired") {
		return err({ code: "invalid_state", status: state.status });
	}
	await deps.bus.emit({
		type: "goal.cancelled",
		version: 1,
		actor,
		data: {
			nodeId: Number(nodeId),
			cancelledBy: actor,
			reason,
		},
		metadata: {},
	});
	return ok(undefined);
}

/** Promote a proposed goal to active. Emits `goal.confirmed`. */
export async function promoteGoal(
	deps: CommandDeps,
	nodeId: NodeId,
	actor: Actor,
): Promise<Result<void, CommandError>> {
	const state = await deps.goals.readState(nodeId);
	if (state === null) return err({ code: "not_found" });
	if (state.status !== "proposed") {
		return err({ code: "invalid_state", status: state.status });
	}
	await deps.bus.emit({
		type: "goal.confirmed",
		version: 1,
		actor,
		data: { nodeId: Number(nodeId), confirmedBy: actor },
		metadata: {},
	});
	return ok(undefined);
}

/** Change priority — emits `goal.priority_changed`. */
export async function setPriority(
	deps: CommandDeps,
	nodeId: NodeId,
	newPriority: number,
	actor: Actor,
	reason = "operator override",
): Promise<Result<void, CommandError>> {
	if (newPriority < 0 || newPriority > 100) {
		return err({
			code: "invalid_argument",
			reason: `priority must be 0..100, got ${String(newPriority)}`,
		});
	}
	const state = await deps.goals.readState(nodeId);
	if (state === null) return err({ code: "not_found" });
	await deps.bus.emit({
		type: "goal.priority_changed",
		version: 1,
		actor,
		data: {
			nodeId: Number(nodeId),
			oldPriority: state.ownerPriority,
			newPriority,
			reason,
		},
		metadata: {},
	});
	return ok(undefined);
}

/** Redact goal fields — CLI-only. Emits `goal.redacted`. */
export async function redactGoal(
	deps: CommandDeps,
	nodeId: NodeId,
	actor: Actor,
	fields: readonly GoalRedactedField[],
	channel: CommandChannel,
): Promise<Result<void, CommandError>> {
	if (channel !== "cli") {
		return err({
			code: "forbidden",
			reason: "/redact is CLI-only",
		});
	}
	const state = await deps.goals.readState(nodeId);
	if (state === null) return err({ code: "not_found" });
	await deps.bus.emit({
		type: "goal.redacted",
		version: 1,
		actor,
		data: {
			nodeId: Number(nodeId),
			redactedFields: fields,
			redactedBy: actor,
		},
		metadata: {},
	});
	return ok(undefined);
}

/** Set autonomy level for a domain — CLI-only. */
export async function setAutonomy(
	deps: CommandDeps,
	domain: string,
	level: number,
	actor: Actor,
	reason: string | null,
	channel: CommandChannel,
): Promise<Result<void, CommandError>> {
	if (channel !== "cli") {
		return err({
			code: "forbidden",
			reason: "/autonomy is CLI-only",
		});
	}
	if (level < 0 || level > 5) {
		return err({
			code: "invalid_argument",
			reason: `level must be 0..5, got ${String(level)}`,
		});
	}
	await deps.goals.setAutonomyPolicy(domain, level, actor, reason);
	return ok(undefined);
}

/** Return the causation chain for a goal — chronological list of events. */
export async function auditGoal(
	deps: CommandDeps,
	nodeId: NodeId,
): Promise<Result<readonly AuditEventRow[], CommandError>> {
	const state = await deps.goals.readState(nodeId);
	if (state === null) return err({ code: "not_found" });
	const events = await deps.goals.auditEvents(nodeId);
	void state;
	return ok(events);
}
