/**
 * Proposal store — staging lifecycle, TTL, GC.
 *
 * A proposal is a staged artifact plus a pending decision. Origins: ideation,
 * reflex, owner_request, executive. The proposal table carries its autonomy
 * requirements (autonomy_domain + required_level), origin trust
 * (effective_trust from the causation chain), and a TTL.
 *
 * State machine:
 *
 *   pending  ──approve──▶ approved ──execute──▶ executed
 *      │                      │
 *      │                      └──reject────▶ rejected
 *      │
 *      └──expire──▶ expired
 *
 * Each transition emits a `proposal.*` event; the projection stays in sync
 * with the event log. The effect of actually creating a workspace artifact
 * (branch, draft) lives in `workspace.ts` — `store.ts` only moves state.
 */

import type { Sql, TransactionSql } from "postgres";
import { monotonicFactory } from "ulid";
import { asQueryable } from "../db/pool.ts";
import type { EventBus } from "../events/bus.ts";
import type { ProposalKind, ProposalOrigin, TrustTierString } from "../events/reflexes.ts";
import type { Actor } from "../events/types.ts";
import type { TrustTier } from "../memory/graph/types.ts";
import { minTier } from "../memory/trust.ts";

const ulid = monotonicFactory();

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ProposalStatus = "pending" | "approved" | "rejected" | "executed" | "expired";

export interface Proposal {
	readonly id: string;
	readonly origin: ProposalOrigin;
	readonly sourceCauseId: string;
	readonly title: string;
	readonly summary: string;
	readonly kind: ProposalKind;
	readonly payload: Record<string, unknown>;
	readonly effectiveTrust: TrustTierString;
	readonly autonomyDomain: string;
	readonly requiredLevel: number;
	readonly status: ProposalStatus;
	readonly workspaceBranch: string | null;
	readonly workspaceDraftId: string | null;
	readonly createdAt: Date;
	readonly expiresAt: Date;
	readonly decidedAt: Date | null;
	readonly decidedBy: string | null;
	readonly redacted: boolean;
}

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

/** Default proposal TTL: 14 days. */
export const DEFAULT_TTL_MS = 14 * 24 * 60 * 60_000;

/** Ideation-origin proposals are capped at autonomy level 2 per §11. */
export const IDEATION_MAX_LEVEL = 2;

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

export interface RequestProposalInput {
	readonly origin: ProposalOrigin;
	readonly sourceCauseId: string;
	readonly title: string;
	readonly summary: string;
	readonly kind: ProposalKind;
	readonly payload: Record<string, unknown>;
	readonly effectiveTrust: TrustTierString;
	readonly autonomyDomain: string;
	readonly requiredLevel: number;
	readonly expiresAt?: Date;
}

/**
 * Insert a proposal row and emit `proposal.requested`. Ideation-origin
 * proposals are capped at autonomy level 2 — callers may request higher
 * but this function enforces the cap.
 *
 * `effectiveTrust` is computed from the causation chain (not taken from
 * caller input) by reading the `sourceCauseId` event's stored
 * `effective_trust_tier` and flooring at the caller's suggested minimum.
 * This prevents trust laundering via a trusted caller asserting a higher
 * tier than the chain actually carries.
 */
export async function requestProposal(
	deps: { readonly sql: Sql; readonly bus: EventBus },
	input: RequestProposalInput,
): Promise<Proposal> {
	const id = ulid();
	const now = new Date();
	const expiresAt = input.expiresAt ?? new Date(now.getTime() + DEFAULT_TTL_MS);
	const requiredLevel =
		input.origin === "ideation"
			? Math.min(input.requiredLevel, IDEATION_MAX_LEVEL)
			: input.requiredLevel;

	return deps.sql.begin(async (tx) => {
		const q = asQueryable(tx);
		// Resolve effective trust from the causation chain so a trusted caller
		// can't assert a higher tier than the source event actually carries.
		const trustRows = await q<{ tier: TrustTier }[]>`
			SELECT effective_trust_tier AS tier FROM events WHERE id = ${input.sourceCauseId}
		`;
		const chainTrust = trustRows[0]?.tier ?? "external";
		const effectiveTrust = minTier(chainTrust, input.effectiveTrust);

		await q`
			INSERT INTO proposal (
				id, origin, source_cause_id, title, summary, kind, payload,
				effective_trust, autonomy_domain, required_level, status, expires_at
			) VALUES (
				${id}, ${input.origin}, ${input.sourceCauseId}, ${input.title},
				${input.summary}, ${input.kind}, ${tx.json(input.payload as never)},
				${effectiveTrust}, ${input.autonomyDomain}, ${requiredLevel},
				'pending', ${expiresAt}
			)
		`;
		await deps.bus.emit(
			{
				type: "proposal.requested",
				version: 1,
				actor: "system",
				data: {
					proposalId: id,
					origin: input.origin,
					kind: input.kind,
					title: input.title,
					summary: input.summary,
					payload: input.payload,
					autonomyDomain: input.autonomyDomain,
					requiredLevel,
					effectiveTrust,
					expiresAt: expiresAt.toISOString(),
				},
				metadata: { causeId: input.sourceCauseId as never },
			},
			{ tx },
		);

		return {
			id,
			origin: input.origin,
			sourceCauseId: input.sourceCauseId,
			title: input.title,
			summary: input.summary,
			kind: input.kind,
			payload: input.payload,
			effectiveTrust,
			autonomyDomain: input.autonomyDomain,
			requiredLevel,
			status: "pending",
			workspaceBranch: null,
			workspaceDraftId: null,
			createdAt: now,
			expiresAt,
			decidedAt: null,
			decidedBy: null,
			redacted: false,
		};
	});
}

/** Approve a pending proposal. Emits `proposal.approved`. */
export async function approveProposal(
	deps: { readonly sql: Sql; readonly bus: EventBus },
	proposalId: string,
	approvedBy: Actor,
): Promise<void> {
	await deps.sql.begin(async (tx) => {
		const q = asQueryable(tx);
		const updated = await q<{ id: string }[]>`
			UPDATE proposal
			SET status = 'approved', decided_at = now(), decided_by = ${approvedBy}
			WHERE id = ${proposalId} AND status = 'pending'
			RETURNING id
		`;
		if (updated.length === 0) return;
		await deps.bus.emit(
			{
				type: "proposal.approved",
				version: 1,
				actor: approvedBy,
				data: { proposalId, approvedBy },
				metadata: {},
			},
			{ tx },
		);
	});
}

/** Reject a pending proposal. Emits `proposal.rejected`. */
export async function rejectProposal(
	deps: { readonly sql: Sql; readonly bus: EventBus },
	proposalId: string,
	rejectedBy: Actor,
	feedback?: string,
): Promise<void> {
	await deps.sql.begin(async (tx) => {
		const q = asQueryable(tx);
		const updated = await q<{ id: string }[]>`
			UPDATE proposal
			SET status = 'rejected', decided_at = now(), decided_by = ${rejectedBy}
			WHERE id = ${proposalId} AND status = 'pending'
			RETURNING id
		`;
		if (updated.length === 0) return;
		await deps.bus.emit(
			{
				type: "proposal.rejected",
				version: 1,
				actor: rejectedBy,
				data: {
					proposalId,
					rejectedBy,
					...(feedback !== undefined ? { feedback } : {}),
				},
				metadata: {},
			},
			{ tx },
		);
	});
}

/**
 * Sweep expired proposals. Transitions `pending` rows whose `expires_at`
 * has passed into `expired` and emits `proposal.expired` for each. Called
 * by a periodic scheduler tick.
 */
export async function sweepExpired(
	deps: { readonly sql: Sql; readonly bus: EventBus },
	at: Date,
): Promise<readonly string[]> {
	return deps.sql.begin(async (tx) => {
		const q = asQueryable(tx);
		const rows = await q<{ id: string }[]>`
			SELECT id FROM proposal
			WHERE status = 'pending' AND expires_at <= ${at}
			FOR UPDATE
		`;
		if (rows.length === 0) return [];
		const ids = rows.map((r) => r.id);
		await q`
			UPDATE proposal SET status = 'expired' WHERE id = ANY(${ids}::text[])
		`;
		for (const id of ids) {
			await deps.bus.emit(
				{
					type: "proposal.expired",
					version: 1,
					actor: "system",
					data: { proposalId: id },
					metadata: {},
				},
				{ tx },
			);
		}
		return ids;
	});
}

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

export async function listPending(sql: Sql | TransactionSql): Promise<readonly Proposal[]> {
	const q = asQueryable(sql);
	const rows = await q<ProposalRow[]>`
		SELECT id, origin, source_cause_id, title, summary, kind, payload,
		       effective_trust, autonomy_domain, required_level, status,
		       workspace_branch, workspace_draft_id, created_at, expires_at,
		       decided_at, decided_by, redacted
		FROM proposal
		WHERE status = 'pending'
		ORDER BY created_at DESC
	`;
	return rows.map(rowToProposal);
}

export async function getProposal(sql: Sql | TransactionSql, id: string): Promise<Proposal | null> {
	const q = asQueryable(sql);
	const rows = await q<ProposalRow[]>`
		SELECT id, origin, source_cause_id, title, summary, kind, payload,
		       effective_trust, autonomy_domain, required_level, status,
		       workspace_branch, workspace_draft_id, created_at, expires_at,
		       decided_at, decided_by, redacted
		FROM proposal WHERE id = ${id}
	`;
	const row = rows[0];
	return row ? rowToProposal(row) : null;
}

type ProposalRow = Record<string, unknown>;

function rowToProposal(row: ProposalRow): Proposal {
	return {
		id: row["id"] as string,
		origin: row["origin"] as ProposalOrigin,
		sourceCauseId: row["source_cause_id"] as string,
		title: row["title"] as string,
		summary: row["summary"] as string,
		kind: row["kind"] as ProposalKind,
		payload: row["payload"] as Record<string, unknown>,
		effectiveTrust: row["effective_trust"] as TrustTierString,
		autonomyDomain: row["autonomy_domain"] as string,
		requiredLevel: row["required_level"] as number,
		status: row["status"] as ProposalStatus,
		workspaceBranch: row["workspace_branch"] as string | null,
		workspaceDraftId: row["workspace_draft_id"] as string | null,
		createdAt: row["created_at"] as Date,
		expiresAt: row["expires_at"] as Date,
		decidedAt: row["decided_at"] as Date | null,
		decidedBy: row["decided_by"] as string | null,
		redacted: row["redacted"] as boolean,
	};
}
