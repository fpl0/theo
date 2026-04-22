/**
 * Operator-command router for the CLI gate.
 *
 * Phase 13b introduced proposal, consent, egress-audit, degradation, and
 * webhook-rotation primitives behind module-level APIs (`src/proposals/`,
 * `src/memory/egress.ts`, `src/memory/cloud_audit.ts`, `src/degradation/`,
 * `src/gates/webhooks/secrets.ts`). This module is the Phase 15 wiring that
 * exposes them as slash commands the owner can issue from the CLI.
 *
 * Every operator command is a *local, synchronous* owner affordance — it
 * acts on the same process through DI-provided repositories, never through
 * the chat engine. Commands emit durable events so the action shows up in
 * the log with full causation.
 */

import type { Sql } from "postgres";
import { readDegradation, setDegradation } from "../../degradation/state.ts";
import type { EventBus } from "../../events/bus.ts";
import type { Actor } from "../../events/types.ts";
import { type AuditWindow, auditCloudEgress } from "../../memory/cloud_audit.ts";
import { grantAutonomousCloudEgress, revokeAutonomousCloudEgress } from "../../memory/egress.ts";
import {
	approveCommand,
	redactCommand,
	rejectCommand,
	summarizePending,
} from "../../proposals/commands.ts";
import type { WebhookSecretStore } from "../webhooks/secrets.ts";

// ---------------------------------------------------------------------------
// Dependencies + result type
// ---------------------------------------------------------------------------

export interface OperatorDeps {
	readonly sql: Sql;
	readonly bus: EventBus;
	readonly webhookSecrets?: WebhookSecretStore;
	readonly actor?: Actor;
}

/** Every command resolves to a short textual summary for the status bar. */
export interface OperatorResult {
	readonly ok: boolean;
	readonly message: string;
}

// ---------------------------------------------------------------------------
// /proposals
// ---------------------------------------------------------------------------

export async function runProposals(deps: OperatorDeps): Promise<OperatorResult> {
	const pending = await summarizePending(deps);
	if (pending.length === 0) return { ok: true, message: "no pending proposals" };
	const lines = pending.map(
		(p) => `  ${p.id}  [L${p.requiredLevel}]  ${p.title}  (expires ${p.expiresAt.toISOString()})`,
	);
	return { ok: true, message: `${pending.length} pending\n${lines.join("\n")}` };
}

// ---------------------------------------------------------------------------
// /approve <id> / /reject <id> [reason] / /redact <id>
// ---------------------------------------------------------------------------

/**
 * Approve a proposal. The CLI looks up the stored `payload_hash` inside the
 * call and forwards it as `expectedPayloadHash` — a concurrent mutation
 * (redaction, edit) between lookup and update fails the approval with
 * `payload_hash_mismatch` so the owner must re-review.
 */
export async function runApprove(
	deps: OperatorDeps,
	args: readonly string[],
): Promise<OperatorResult> {
	const id = args[0];
	if (id === undefined) return { ok: false, message: "usage: /approve <id>" };
	const actor = deps.actor ?? "user";
	// Resolve the currently-stored payload_hash so the approve call binds to it.
	const rows = await deps.sql<Record<string, unknown>[]>`
		SELECT payload_hash FROM proposal WHERE id = ${id}
	`;
	const stored = rows[0]?.["payload_hash"] as string | undefined;
	if (stored === undefined) return { ok: false, message: `not found: ${id}` };
	const result = await approveCommand(deps, id, actor, stored);
	if (!result.ok) return { ok: false, message: `reject: ${result.reason}` };
	return { ok: true, message: `approved ${id}` };
}

export async function runReject(
	deps: OperatorDeps,
	args: readonly string[],
): Promise<OperatorResult> {
	const [id, ...rest] = args;
	if (id === undefined) return { ok: false, message: "usage: /reject <id> [reason]" };
	const feedback = rest.length > 0 ? rest.join(" ") : undefined;
	const actor = deps.actor ?? "user";
	const result = await rejectCommand(deps, id, actor, feedback);
	if (!result.ok) return { ok: false, message: `reject: ${result.reason}` };
	return { ok: true, message: `rejected ${id}` };
}

export async function runRedact(
	deps: OperatorDeps,
	args: readonly string[],
): Promise<OperatorResult> {
	const id = args[0];
	if (id === undefined) return { ok: false, message: "usage: /redact <id>" };
	const actor = deps.actor ?? "user";
	const result = await redactCommand(deps, id, actor);
	if (!result.ok) return { ok: false, message: `redact: ${result.reason}` };
	return { ok: true, message: `redacted ${id}` };
}

// ---------------------------------------------------------------------------
// /consent grant|revoke [reason]
// ---------------------------------------------------------------------------

export async function runConsent(
	deps: OperatorDeps,
	args: readonly string[],
): Promise<OperatorResult> {
	const verb = args[0];
	if (verb !== "grant" && verb !== "revoke") {
		return { ok: false, message: "usage: /consent grant|revoke [reason]" };
	}
	const actor = deps.actor ?? "user";
	const reason = args.slice(1).join(" ").trim() || undefined;
	if (verb === "grant") {
		await grantAutonomousCloudEgress(deps, actor, reason);
		return { ok: true, message: "autonomous cloud egress ENABLED" };
	}
	await revokeAutonomousCloudEgress(deps, actor);
	return { ok: true, message: "autonomous cloud egress DISABLED" };
}

// ---------------------------------------------------------------------------
// /cloud-audit [day|week|month]
// ---------------------------------------------------------------------------

export async function runCloudAudit(
	deps: OperatorDeps,
	args: readonly string[],
): Promise<OperatorResult> {
	const raw = args[0] ?? "day";
	if (raw !== "day" && raw !== "week" && raw !== "month") {
		return { ok: false, message: "usage: /cloud-audit with window day / week / month" };
	}
	const window: AuditWindow = raw;
	const entries = await auditCloudEgress(deps.sql, window);
	if (entries.length === 0) return { ok: true, message: `no cloud egress in last ${window}` };
	const total = entries.reduce((acc, e) => acc + e.costUsd, 0);
	const lines = entries.map(
		(e) =>
			`  ${e.turnClass}: ${e.turns} turns, $${e.costUsd.toFixed(4)}, ${e.inputTokens}→${e.outputTokens} tokens`,
	);
	return {
		ok: true,
		message: `cloud egress (${window}): $${total.toFixed(4)} across ${entries.length} classes\n${lines.join("\n")}`,
	};
}

// ---------------------------------------------------------------------------
// /degradation [0-4] [reason]
// ---------------------------------------------------------------------------

export async function runDegradation(
	deps: OperatorDeps,
	args: readonly string[],
): Promise<OperatorResult> {
	if (args.length === 0) {
		const state = await readDegradation(deps.sql);
		return {
			ok: true,
			message: `level: L${state.level} — ${state.reason} (since ${state.changedAt.toISOString()})`,
		};
	}
	const raw = args[0] ?? "";
	const parsed = Number.parseInt(raw, 10);
	if (!Number.isInteger(parsed) || parsed < 0 || parsed > 4) {
		return { ok: false, message: "usage: /degradation [0-4] [reason]" };
	}
	const level = parsed as 0 | 1 | 2 | 3 | 4;
	const reason = args.slice(1).join(" ").trim() || "manual";
	const actor = deps.actor ?? "user";
	const state = await setDegradation(deps, level, reason, actor);
	return { ok: true, message: `degradation set to L${state.level} — ${state.reason}` };
}

// ---------------------------------------------------------------------------
// /webhook-rotate <source>
// ---------------------------------------------------------------------------

/**
 * Rotate a source's webhook secret. The store generates a fresh random
 * secret and returns it once — the CLI surfaces it so the owner can paste
 * it into the remote (GitHub, Linear, email relay). After the 7-day grace
 * window the previous secret is swept automatically.
 */
export async function runWebhookRotate(
	deps: OperatorDeps,
	args: readonly string[],
): Promise<OperatorResult> {
	if (deps.webhookSecrets === undefined) {
		return { ok: false, message: "webhook gate disabled — no secret store configured" };
	}
	const source = args[0];
	if (source === undefined) {
		return { ok: false, message: "usage: /webhook-rotate <source>" };
	}
	const actor = deps.actor ?? "user";
	const fresh = await deps.webhookSecrets.rotate(source, actor);
	return {
		ok: true,
		message: `rotated ${source} — new secret (copy to remote, shown once): ${fresh}`,
	};
}

// ---------------------------------------------------------------------------
// Dispatcher
// ---------------------------------------------------------------------------

/** Name → handler. The App imports this map and dispatches in one place. */
export const OPERATOR_COMMANDS = {
	"/proposals": runProposals,
	"/approve": runApprove,
	"/reject": runReject,
	"/redact": runRedact,
	"/consent": runConsent,
	"/cloud-audit": runCloudAudit,
	"/degradation": runDegradation,
	"/webhook-rotate": runWebhookRotate,
} as const satisfies Record<
	string,
	(deps: OperatorDeps, args: readonly string[]) => Promise<OperatorResult>
>;

export type OperatorCommandName = keyof typeof OPERATOR_COMMANDS;

/** True when `name` is a recognized operator command. */
export function isOperatorCommand(name: string): name is OperatorCommandName {
	return name in OPERATOR_COMMANDS;
}

/**
 * Parse a raw input line into the command name and positional args.
 * `/approve abc foo` → `{ name: "/approve", args: ["abc", "foo"] }`.
 * Returns null when the input doesn't start with a slash.
 */
export function parseOperatorLine(
	input: string,
): { readonly name: string; readonly args: readonly string[] } | null {
	const trimmed = input.trim();
	if (!trimmed.startsWith("/")) return null;
	const parts = trimmed.split(/\s+/u);
	const name = parts[0] ?? "";
	return { name, args: parts.slice(1) };
}

/** Run an operator command line. Unknown commands return an error result. */
export async function runOperatorCommand(
	deps: OperatorDeps,
	input: string,
): Promise<OperatorResult> {
	const parsed = parseOperatorLine(input);
	if (parsed === null) return { ok: false, message: "not a command" };
	if (!isOperatorCommand(parsed.name)) {
		return { ok: false, message: `unknown command: ${parsed.name}` };
	}
	return OPERATOR_COMMANDS[parsed.name](deps, parsed.args);
}
