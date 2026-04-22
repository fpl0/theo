/**
 * Owner-facing proposal commands.
 *
 * Thin wrappers around `store.ts` that carry a textual outcome for the CLI
 * layer. Commands are synchronous in the event log — they write a
 * `proposal.*` event plus a projection update in a single transaction.
 *
 * CLI wiring (binding to actual `/approve` / `/reject` / `/proposals` slash
 * commands) lives in `src/gates/cli/` — Phase 15 operationalization. These
 * functions are written first so the tests and MCP plumbing can exercise
 * the happy paths end-to-end.
 */

import type { Sql } from "postgres";
import type { EventBus } from "../events/bus.ts";
import type { EventId } from "../events/ids.ts";
import type { Actor } from "../events/types.ts";
import {
	approveProposal,
	executeProposal,
	getProposal,
	listPending,
	type Proposal,
	redactProposal,
	rejectProposal,
	requestProposal,
} from "./store.ts";

export interface CommandDeps {
	readonly sql: Sql;
	readonly bus: EventBus;
}

// ---------------------------------------------------------------------------
// `/proposals`
// ---------------------------------------------------------------------------

export interface ProposalSummary {
	readonly id: string;
	readonly title: string;
	readonly origin: string;
	readonly requiredLevel: number;
	readonly expiresAt: Date;
}

export async function summarizePending(deps: CommandDeps): Promise<readonly ProposalSummary[]> {
	const pending = await listPending(deps.sql);
	return pending.map((p) => ({
		id: p.id,
		title: p.title,
		origin: p.origin,
		requiredLevel: p.requiredLevel,
		expiresAt: p.expiresAt,
	}));
}

// ---------------------------------------------------------------------------
// `/approve <id>` / `/reject <id>`
// ---------------------------------------------------------------------------

export type CommandResult =
	| { readonly ok: true; readonly proposal: Proposal }
	| { readonly ok: false; readonly reason: string };

/**
 * Approve a pending proposal.
 *
 * `expectedPayloadHash` binds the approval to the exact payload the owner
 * reviewed. The CLI captures the stored hash when it lists proposals and
 * passes it back here; if the hash changed between list-time and approve-
 * time (e.g., a concurrent redaction or bug in the emitter), the approval
 * is refused with `payload_hash_mismatch`.
 */
export async function approveCommand(
	deps: CommandDeps,
	id: string,
	actor: Actor = "user",
	expectedPayloadHash?: string,
): Promise<CommandResult> {
	const existing = await getProposal(deps.sql, id);
	if (!existing) return { ok: false, reason: "not_found" };
	if (existing.status !== "pending") {
		return { ok: false, reason: `already ${existing.status}` };
	}
	const outcome = await approveProposal(deps, id, actor, expectedPayloadHash);
	if (outcome.kind === "payload_hash_mismatch") {
		return { ok: false, reason: "payload_hash_mismatch" };
	}
	if (outcome.kind === "not_pending") {
		return { ok: false, reason: "not_pending" };
	}
	const reloaded = await getProposal(deps.sql, id);
	return reloaded ? { ok: true, proposal: reloaded } : { ok: false, reason: "vanished" };
}

export async function rejectCommand(
	deps: CommandDeps,
	id: string,
	actor: Actor = "user",
	feedback?: string,
): Promise<CommandResult> {
	const existing = await getProposal(deps.sql, id);
	if (!existing) return { ok: false, reason: "not_found" };
	if (existing.status !== "pending") {
		return { ok: false, reason: `already ${existing.status}` };
	}
	await rejectProposal(deps, id, actor, feedback);
	const reloaded = await getProposal(deps.sql, id);
	return reloaded ? { ok: true, proposal: reloaded } : { ok: false, reason: "vanished" };
}

/** Redact a proposal's sensitive contents (sets redacted=true, clears payload). */
export async function redactCommand(
	deps: CommandDeps,
	id: string,
	actor: Actor = "user",
): Promise<CommandResult> {
	const existing = await getProposal(deps.sql, id);
	if (!existing) return { ok: false, reason: "not_found" };
	if (existing.redacted) return { ok: false, reason: "already_redacted" };
	await redactProposal(deps, id, actor);
	const reloaded = await getProposal(deps.sql, id);
	return reloaded ? { ok: true, proposal: reloaded } : { ok: false, reason: "vanished" };
}

/**
 * Mark an approved proposal as executed with the outcome event ids.
 * Typically invoked by the effect handler that materialized the proposal,
 * not by the CLI directly; exposed here for symmetry with approve/reject.
 */
export async function executeCommand(
	deps: CommandDeps,
	id: string,
	outcomeEventIds: readonly EventId[],
): Promise<CommandResult> {
	const existing = await getProposal(deps.sql, id);
	if (!existing) return { ok: false, reason: "not_found" };
	if (existing.status !== "approved") {
		return { ok: false, reason: `not approved (${existing.status})` };
	}
	await executeProposal(deps, id, outcomeEventIds);
	const reloaded = await getProposal(deps.sql, id);
	return reloaded ? { ok: true, proposal: reloaded } : { ok: false, reason: "vanished" };
}

// Re-export requestProposal for owner-initiated proposals (e.g., `/draft`).
export { requestProposal };
