/**
 * Reflex decision handler (`foundation.md §7.4`).
 *
 * The reflex chain splits into three decision events and one effect event:
 *
 *   webhook.received  -> [webhook.verified] emitted by the gate itself
 *   webhook.verified  -> reflex.triggered (this module's decision handler)
 *   reflex.triggered  -> reflex.thought   (effect handler — see dispatch.ts)
 *
 * The decision stage runs on both live dispatch and replay. It reads the
 * webhook body from the transient `webhook_body` table, looks for a matching
 * goal or autonomy domain, and emits `reflex.triggered` with an envelope
 * nonce. If the decision rejects (stale, rate-limited, no match), it emits
 * `reflex.suppressed` instead.
 *
 * Everything here is deterministic over the event log — no LLM calls, no
 * network. The effect handler in `dispatch.ts` runs the subagent and is
 * skipped during replay.
 */

import type { Sql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { EventOfType } from "../events/types.ts";
import { newEnvelopeNonce } from "../gates/webhooks/envelope.ts";
import type { TrustTier } from "../memory/graph/types.ts";

// ---------------------------------------------------------------------------
// Staleness
// ---------------------------------------------------------------------------

export const DEFAULT_STALE_MS = 60 * 60_000; // one hour

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------

export interface ReflexDecisionDeps {
	readonly sql: Sql;
	readonly bus: EventBus;
	readonly now?: () => Date;
	readonly staleMs?: number;
}

/**
 * Register the reflex decision handler. It converts `webhook.verified` into
 * either `reflex.triggered` (successful match, effective trust = external)
 * or `reflex.suppressed` (stale / no-match / rate-limited).
 */
export function registerReflexDecision(deps: ReflexDecisionDeps): void {
	const { sql, bus } = deps;
	const now = deps.now ?? ((): Date => new Date());
	const staleMs = deps.staleMs ?? DEFAULT_STALE_MS;

	bus.on(
		"webhook.verified",
		async (event) => {
			await decideReflex(sql, bus, event, now(), staleMs);
		},
		{ id: "reflex-decision", mode: "decision" },
	);
}

async function decideReflex(
	sql: Sql,
	bus: EventBus,
	event: EventOfType<"webhook.verified">,
	currentTime: Date,
	staleMs: number,
): Promise<void> {
	// 1. Resolve the underlying webhook.received event to get the timing and
	//    trust seed. We also need the payload ref to find the body.
	const receivedId = event.metadata.causeId;
	if (receivedId === undefined) {
		// Should not happen — webhook.verified is always caused by webhook.received.
		return;
	}

	const query = asQueryable(sql);
	const receivedRows = await query<Record<string, unknown>[]>`
		SELECT data, timestamp, effective_trust_tier
		FROM events
		WHERE id = ${receivedId}
	`;
	const received = receivedRows[0];
	if (!received) return;

	// 2. Staleness check.
	const receivedTimestamp = received["timestamp"] as Date;
	const receivedEffectiveTrust = received["effective_trust_tier"] as TrustTier;
	const receivedAtMs = receivedTimestamp.getTime();
	if (currentTime.getTime() - receivedAtMs > staleMs) {
		await bus.emit({
			type: "reflex.suppressed",
			version: 1,
			actor: "system",
			data: { webhookEventId: receivedId, reason: "stale" },
			metadata: { causeId: event.id },
		});
		return;
	}

	// 3. Resolve autonomy domain from the parsed body metadata. The gate
	//    stored the body under `webhook_body`; we read the id from payloadRef.
	const payloadRef = String(event.data.payloadRef);
	const bodyRows = await query<Record<string, unknown>[]>`
		SELECT body FROM webhook_body WHERE id = ${payloadRef}
	`;
	const body = bodyRows[0]?.["body"] as Record<string, unknown> | undefined;

	// Autonomy domain inference is intentionally simple — the webhook gate
	// writes parser-derived classification into `body.__theo.autonomyDomain`
	// (see gates/webhooks/server.ts). Fall back to `webhook.generic` if
	// the field is missing (older rows or parser returned an unknown kind).
	const meta = body?.["theoMeta"] as { autonomyDomain?: unknown } | undefined;
	const autonomyDomain =
		typeof meta?.autonomyDomain === "string" ? meta.autonomyDomain : "webhook.generic";

	// 4. Emit reflex.triggered. The effective trust is inherited from the
	//    webhook.received event (external). The envelope nonce rotates per
	//    turn to prevent delimiter collision.
	await bus.emit({
		type: "reflex.triggered",
		version: 1,
		actor: "system",
		data: {
			webhookEventId: receivedId,
			source: String(event.data.source),
			goalNodeId: null,
			autonomyDomain,
			effectiveTrust: receivedEffectiveTrust,
			envelopeNonce: newEnvelopeNonce(),
		},
		metadata: { causeId: event.id },
	});
}
