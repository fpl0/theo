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
import type { Actor } from "../events/types.ts";
import {
	approveProposal,
	getProposal,
	listPending,
	type Proposal,
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

export async function approveCommand(
	deps: CommandDeps,
	id: string,
	actor: Actor = "user",
): Promise<CommandResult> {
	const existing = await getProposal(deps.sql, id);
	if (!existing) return { ok: false, reason: "not_found" };
	if (existing.status !== "pending") {
		return { ok: false, reason: `already ${existing.status}` };
	}
	await approveProposal(deps, id, actor);
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

// Re-export requestProposal for owner-initiated proposals (e.g., `/draft`).
export { requestProposal };
