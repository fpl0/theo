/**
 * Egress privacy filter (`foundation.md §7.8`).
 *
 * Every user-model dimension carries `egress_sensitivity` — `public` |
 * `private` | `local_only`. Outgoing prompts are filtered at the `query()`
 * call site based on the turn class:
 *
 *   * interactive : include public + private (local_only never leaves)
 *   * reflex      : include public only (private stripped)
 *   * executive   : include public only (private stripped)
 *   * ideation    : include public only (private stripped)
 *
 * `local_only` dimensions never reach the cloud regardless of class.
 *
 * In addition, autonomous classes (non-interactive) require an active
 * consent grant (`autonomous_cloud_egress.enabled`). Without the grant the
 * filter blocks the whole turn with `reason: "no_consent"` — this is a
 * hard stop, not a strip.
 */

import type { Sql, TransactionSql } from "postgres";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { TurnClass } from "../events/reflexes.ts";
import type { Actor } from "../events/types.ts";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type EgressSensitivity = "public" | "private" | "local_only";

export interface DimensionWithSensitivity {
	readonly name: string;
	readonly egressSensitivity: EgressSensitivity;
}

/** Inbound prompt bundle handed to the filter. Only the pieces that vary. */
export interface AssembledPromptForEgress {
	readonly userModelDimensions: readonly DimensionWithSensitivity[];
}

/** Outcome of the filter. `strippedDimensions` is always present for audit. */
export type EgressDecision =
	| {
			readonly allowed: true;
			readonly strippedDimensions: readonly string[];
			readonly includedDimensions: readonly string[];
	  }
	| {
			readonly allowed: false;
			readonly strippedDimensions: readonly string[];
			readonly includedDimensions: readonly string[];
			readonly reason: "no_consent" | "local_only_only";
	  };

export interface ConsentState {
	readonly autonomousCloudEgressEnabled: boolean;
}

// ---------------------------------------------------------------------------
// Pure decision function
// ---------------------------------------------------------------------------

/**
 * Decide what to include / strip for an outgoing prompt. Pure over its
 * inputs; no I/O.
 *
 * Returns `allowed: false` when:
 *   - The turn is autonomous and the consent ledger does not grant
 *     autonomous cloud egress.
 *
 * The `strippedDimensions` and `includedDimensions` lists are always
 * populated so the caller can emit `cloud_egress.turn` for audit.
 */
export function filterOutgoingPrompt(
	prompt: AssembledPromptForEgress,
	turnClass: TurnClass,
	consent: ConsentState,
): EgressDecision {
	const stripped: string[] = [];
	const included: string[] = [];

	for (const dim of prompt.userModelDimensions) {
		if (dim.egressSensitivity === "local_only") {
			stripped.push(dim.name);
			continue;
		}
		if (dim.egressSensitivity === "private" && turnClass !== "interactive") {
			stripped.push(dim.name);
			continue;
		}
		included.push(dim.name);
	}

	if (turnClass !== "interactive" && !consent.autonomousCloudEgressEnabled) {
		return {
			allowed: false,
			strippedDimensions: stripped,
			includedDimensions: included,
			reason: "no_consent",
		};
	}

	return {
		allowed: true,
		strippedDimensions: stripped,
		includedDimensions: included,
	};
}

// ---------------------------------------------------------------------------
// Consent ledger projection
// ---------------------------------------------------------------------------

/** Policy key for autonomous cloud egress — the only gate in Phase 13b. */
export const AUTONOMOUS_CLOUD_EGRESS = "autonomous_cloud_egress";

/** Read the current consent state from the ledger. */
export async function readConsent(sql: Sql | TransactionSql): Promise<ConsentState> {
	const query = asQueryable(sql);
	const rows = await query<{ enabled: boolean }[]>`
		SELECT enabled FROM consent_ledger WHERE policy = ${AUTONOMOUS_CLOUD_EGRESS}
	`;
	return { autonomousCloudEgressEnabled: rows[0]?.enabled ?? false };
}

// ---------------------------------------------------------------------------
// Owner commands
// ---------------------------------------------------------------------------

/** Grant consent for autonomous cloud egress. */
export async function grantAutonomousCloudEgress(
	deps: { readonly sql: Sql; readonly bus: EventBus },
	grantedBy: Actor,
	reason?: string,
): Promise<void> {
	await deps.sql.begin(async (tx) => {
		const q = asQueryable(tx);
		await q`
			INSERT INTO consent_ledger (policy, enabled, granted_by, reason)
			VALUES (${AUTONOMOUS_CLOUD_EGRESS}, true, ${grantedBy}, ${reason ?? null})
			ON CONFLICT (policy) DO UPDATE SET
				enabled = true,
				granted_by = ${grantedBy},
				granted_at = now(),
				reason = ${reason ?? null}
		`;
		await deps.bus.emit(
			{
				type: "policy.autonomous_cloud_egress.enabled",
				version: 1,
				actor: grantedBy,
				data: {
					policy: AUTONOMOUS_CLOUD_EGRESS,
					grantedBy,
					...(reason !== undefined ? { reason } : {}),
				},
				metadata: {},
			},
			{ tx },
		);
	});
}

/** Revoke consent for autonomous cloud egress. */
export async function revokeAutonomousCloudEgress(
	deps: { readonly sql: Sql; readonly bus: EventBus },
	revokedBy: Actor,
): Promise<void> {
	await deps.sql.begin(async (tx) => {
		const q = asQueryable(tx);
		await q`
			INSERT INTO consent_ledger (policy, enabled, granted_by)
			VALUES (${AUTONOMOUS_CLOUD_EGRESS}, false, ${revokedBy})
			ON CONFLICT (policy) DO UPDATE SET
				enabled = false,
				granted_by = ${revokedBy},
				granted_at = now()
		`;
		await deps.bus.emit(
			{
				type: "policy.autonomous_cloud_egress.disabled",
				version: 1,
				actor: revokedBy,
				data: { policy: AUTONOMOUS_CLOUD_EGRESS, revokedBy },
				metadata: {},
			},
			{ tx },
		);
	});
}
