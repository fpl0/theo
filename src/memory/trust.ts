/**
 * Causation-chain effective trust walker (`foundation.md §7.3`).
 *
 * Every durable event stores `effective_trust_tier = min(actor_trust,
 * parent.effective_trust)` — the tier is *not* just the actor's tier, it is
 * the minimum across the event's whole causation chain. Walking the parent
 * is O(1) because the parent's effective tier is already stored, so the
 * walk collapses to a single SELECT per emit.
 *
 * This prevents trust laundering: a webhook-sourced content item cannot be
 * copied into the graph by a trusted actor and "bleach" its external tier
 * away. Any event whose ancestry touches external content inherits
 * `external` (or lower).
 *
 * Depth is bounded at 10. Deeper chains are forced to `external` — a
 * defence against cycles the `causeId` chain should not have but which a
 * bug could introduce.
 */

import type { Sql, TransactionSql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventId } from "../events/ids.ts";
import type { Actor, EventMetadata } from "../events/types.ts";
import type { TrustTier } from "./graph/types.ts";

// ---------------------------------------------------------------------------
// Ordered tier table
// ---------------------------------------------------------------------------

/**
 * Tiers ordered from most privileged (owner) to least (untrusted). Index is
 * the tier's rank — smaller = stronger. `minTier` picks the weaker tier
 * (the larger index).
 */
const TRUST_ORDER: readonly TrustTier[] = [
	"owner",
	"owner_confirmed",
	"verified",
	"inferred",
	"external",
	"untrusted",
] as const;

const TRUST_RANK: Readonly<Record<TrustTier, number>> = {
	owner: 0,
	owner_confirmed: 1,
	verified: 2,
	inferred: 3,
	external: 4,
	untrusted: 5,
};

/** The weaker of two tiers — higher rank wins. */
export function minTier(a: TrustTier, b: TrustTier): TrustTier {
	return TRUST_RANK[a] > TRUST_RANK[b] ? a : b;
}

/** Return every tier that is strictly weaker than `tier`. Used for filters. */
export function weakerThan(tier: TrustTier): readonly TrustTier[] {
	const rank = TRUST_RANK[tier];
	return TRUST_ORDER.filter((t) => TRUST_RANK[t] > rank);
}

/** True when `tier` is external or untrusted. */
export function isExternalTier(tier: TrustTier): boolean {
	return TRUST_RANK[tier] >= TRUST_RANK.external;
}

// ---------------------------------------------------------------------------
// Actor → tier mapping
// ---------------------------------------------------------------------------

/**
 * Default tier for an actor kind. Gates override this by stamping their own
 * tier on the caller's behalf (e.g., the CLI grants `owner`, Telegram
 * grants `verified`). Webhooks always land as `external` via the gate.
 */
export function actorTrust(actor: Actor): TrustTier {
	switch (actor) {
		case "user":
			return "owner";
		case "theo":
			return "owner_confirmed";
		case "scheduler":
			return "owner_confirmed";
		case "system":
			return "owner";
	}
}

// ---------------------------------------------------------------------------
// Walker
// ---------------------------------------------------------------------------

/** Hard cap on causation-chain walks. Deeper → forced `external`. */
export const MAX_CAUSATION_DEPTH = 10;

/**
 * Compute effective trust for an event that is about to be emitted. The
 * event's actor defines the starting tier; if `metadata.causeId` is set,
 * we walk up the chain reading each parent's stored `effective_trust_tier`
 * and taking the minimum.
 *
 * Because every prior event already has its effective tier stored, the walk
 * rarely exceeds a few hops — the parent's stored value already represents
 * its own ancestry.
 *
 * Caller options:
 *   - `override` — force a specific tier regardless of the walk. Used by
 *     owner commands that elevate a proposal-origin event (e.g. `/approve`
 *     promotes an ideation proposal to `owner` for the resulting
 *     `goal.confirmed` event).
 *   - `seedTier` — override the actor's default tier (e.g., the webhook
 *     gate emits `webhook.received` at `external` regardless of actor).
 */
export interface EffectiveTrustOptions {
	readonly override?: TrustTier;
	readonly seedTier?: TrustTier;
	readonly maxDepth?: number;
}

export async function computeEffectiveTrust(
	sql: Sql | TransactionSql,
	actor: Actor,
	metadata: EventMetadata,
	options: EffectiveTrustOptions = {},
): Promise<TrustTier> {
	if (options.override !== undefined) {
		return options.override;
	}

	const maxDepth = options.maxDepth ?? MAX_CAUSATION_DEPTH;
	let tier: TrustTier = options.seedTier ?? actorTrust(actor);
	let currentCauseId = metadata.causeId;
	let depth = 0;

	const query = asQueryable(sql);

	while (currentCauseId !== undefined && depth < maxDepth) {
		const rows = await query<Record<string, unknown>[]>`
			SELECT effective_trust_tier,
			       (metadata->>'causeId')::text AS cause_id
			FROM events
			WHERE id = ${currentCauseId}
		`;
		const row = rows[0];
		if (!row) break;

		tier = minTier(tier, row["effective_trust_tier"] as TrustTier);
		currentCauseId = ((row["cause_id"] as string | null) ?? undefined) as EventId | undefined;
		depth += 1;
	}

	// Depth exceeded while still walking → the chain is suspicious.
	// Forcing `external` here prevents a cycle or an absurd chain from
	// laundering external content into a trusted tier.
	if (depth >= maxDepth && currentCauseId !== undefined) {
		return minTier(tier, "external");
	}

	return tier;
}
