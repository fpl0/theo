/**
 * Reflex effect handler — runs the subagent turn and emits `reflex.thought`.
 *
 * This handler is registered with `mode: "effect"`. The bus fast-forwards
 * its cursor on startup so historical webhooks do not re-run LLM calls;
 * during live dispatch the handler:
 *
 *   1. Reads the webhook body from `webhook_body` for the envelope.
 *   2. Wraps the body in the external envelope with per-turn nonce.
 *   3. Dispatches the `scanner` subagent with the external-tier tool
 *      allowlist (no write tools, no file system, no network).
 *   4. Captures the outcome as a durable `reflex.thought` event so
 *      downstream decision handlers can read it deterministically on
 *      replay.
 *
 * Tool allowlist enforcement is the load-bearing security control for
 * untrusted webhook content. External-tier turns must never emit memory
 * writes, goal updates, or code changes directly — they produce
 * `proposal.requested` through the downstream decision handler.
 */

import type { Sql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { ReflexOutcome } from "../events/reflexes.ts";
import type { EventOfType } from "../events/types.ts";
import { wrapExternal } from "../gates/webhooks/envelope.ts";

// ---------------------------------------------------------------------------
// External tool allowlist (foundation.md §7.6)
// ---------------------------------------------------------------------------

/**
 * Read-only allowlist used by reflex dispatch. Any attempt by the subagent
 * to call a tool outside this list must be rejected by the dispatcher.
 * Built-in SDK tools (Bash, Read, Write, Edit, WebFetch, WebSearch) are
 * intentionally excluded — external turns may not act on the file system.
 */
export const EXTERNAL_TURN_TOOLS: readonly string[] = [
	"mcp__memory__search_memory",
	"mcp__memory__search_skills",
	"mcp__memory__read_core",
	"mcp__memory__read_goals",
] as const;

// ---------------------------------------------------------------------------
// Dispatcher
// ---------------------------------------------------------------------------

/**
 * A reflex subagent runner. The engine supplies a real runner (wiring
 * `@anthropic-ai/claude-agent-sdk`); tests substitute a deterministic
 * fake. The runner returns both a structured outcome and the usage
 * bookkeeping required for `reflex.thought`.
 */
export interface ReflexRunner {
	run(input: ReflexRunInput): Promise<ReflexRunResult>;
}

export interface ReflexRunInput {
	readonly envelope: string;
	readonly nonce: string;
	readonly source: string;
	readonly allowedTools: readonly string[];
	readonly subagent: string;
	readonly model: string;
	readonly advisorModel?: string | undefined;
}

export interface ReflexRunResult {
	readonly subagent: string;
	readonly model: string;
	readonly advisorModel?: string | undefined;
	readonly inputTokens: number;
	readonly outputTokens: number;
	readonly costUsd: number;
	readonly iterations: readonly {
		readonly kind: "executor" | "advisor_message";
		readonly model: string;
		readonly inputTokens: number;
		readonly outputTokens: number;
		readonly costUsd: number;
	}[];
	readonly outcome: ReflexOutcome;
}

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------

export interface ReflexDispatchDeps {
	readonly sql: Sql;
	readonly bus: EventBus;
	readonly runner: ReflexRunner;
	/** Reflex-speed subagent. Default: `scanner` (Haiku, no advisor). */
	readonly subagent?: string;
	readonly model?: string;
}

export function registerReflexDispatch(deps: ReflexDispatchDeps): void {
	const { sql, bus, runner } = deps;
	const subagent = deps.subagent ?? "scanner";
	const model = deps.model ?? "haiku";

	bus.on(
		"reflex.triggered",
		async (event) => {
			await dispatchReflex(sql, bus, runner, subagent, model, event);
		},
		{ id: "reflex-dispatch", mode: "effect" },
	);
}

async function dispatchReflex(
	sql: Sql,
	bus: EventBus,
	runner: ReflexRunner,
	subagent: string,
	model: string,
	event: EventOfType<"reflex.triggered">,
): Promise<void> {
	// Fetch the verified event so we can locate the webhook body.
	const query = asQueryable(sql);
	const verifiedRows = await query<{ data: Record<string, unknown> }[]>`
		SELECT data FROM events WHERE id = ${event.metadata.causeId ?? ""}
	`;
	const verified = verifiedRows[0];
	if (!verified) return;
	const payloadRef = String(verified.data["payloadRef"]);

	const bodyRows = await query<{ body: Record<string, unknown> }[]>`
		SELECT body FROM webhook_body WHERE id = ${payloadRef}
	`;
	const body = bodyRows[0]?.body;
	if (!body) {
		// Body expired — suppress and return.
		await bus.emit({
			type: "reflex.suppressed",
			version: 1,
			actor: "system",
			data: { webhookEventId: event.data.webhookEventId, reason: "no_match" },
			metadata: { causeId: event.id },
		});
		return;
	}

	// Minimally serialize body to a safe string for the envelope.
	const contentStr = JSON.stringify(body, null, 2);
	const envelope = wrapExternal(contentStr, event.data.source, event.data.envelopeNonce);

	const result = await runner.run({
		envelope: envelope.wrapped,
		nonce: envelope.nonce,
		source: event.data.source,
		allowedTools: EXTERNAL_TURN_TOOLS,
		subagent,
		model,
	});

	await bus.emit({
		type: "reflex.thought",
		version: 1,
		actor: "theo",
		data: {
			reflexEventId: event.id,
			webhookEventId: event.data.webhookEventId,
			subagent: result.subagent,
			model: result.model,
			...(result.advisorModel !== undefined ? { advisorModel: result.advisorModel } : {}),
			inputTokens: result.inputTokens,
			outputTokens: result.outputTokens,
			iterations: result.iterations,
			costUsd: result.costUsd,
			outcome: result.outcome,
		},
		metadata: { causeId: event.id },
	});
}
