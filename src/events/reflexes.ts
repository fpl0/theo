/**
 * Event types for Phase 13b: webhook gate, reflexes, ideation, proposals,
 * egress filter, and degradation ladder.
 *
 * 27 new event types across six groups. All at version 1 — the foundation
 * plan forbids pre-production upcasters. Every event is a durable
 * `TheoEvent<Type, Data>`; no ephemeral events are introduced here.
 *
 * Secret material is categorically absent — webhook secrets live only in
 * the `webhook_secret` table and rotation events carry no key material.
 */

import type { TrustTier } from "../memory/graph/types.ts";
import type { EventId } from "./ids.ts";
import type { Actor, TheoEvent } from "./types.ts";

// ---------------------------------------------------------------------------
// Shared shapes
// ---------------------------------------------------------------------------

/** Turn class used by the egress filter and audit log. */
export type TurnClass = "interactive" | "reflex" | "executive" | "ideation";

/**
 * One iteration of an SDK turn — either an executor call or an advisor
 * call. Mirrors the shape Phase 14 already uses; duplicated here so Phase
 * 13b events do not take a cross-module type dependency.
 */
export interface IterationSummary {
	readonly kind: "executor" | "advisor_message";
	readonly model: string;
	readonly inputTokens: number;
	readonly outputTokens: number;
	readonly costUsd: number;
}

// ---------------------------------------------------------------------------
// Webhook events
// ---------------------------------------------------------------------------

export interface WebhookReceivedData {
	readonly source: string;
	readonly deliveryId: string;
	readonly bodyHash: string;
	readonly bodyByteLength: number;
	readonly receivedAt: string;
}

export interface WebhookVerifiedData {
	readonly source: string;
	readonly deliveryId: string;
	readonly payloadRef: string; // points at webhook_body.id
}

export interface WebhookRejectedData {
	readonly source: string;
	readonly deliveryId: string | null;
	readonly reason:
		| "parse_error"
		| "signature_invalid"
		| "body_too_large"
		| "unknown_source"
		| "duplicate"
		| "stale";
}

export interface WebhookRateLimitedData {
	readonly source: string;
	readonly retryAfterSec: number;
}

export interface WebhookSecretRotatedData {
	readonly source: string;
	readonly rotatedBy: Actor;
}

export interface WebhookSecretGraceExpiredData {
	readonly source: string;
}

// ---------------------------------------------------------------------------
// Reflex events
// ---------------------------------------------------------------------------

export interface ReflexTriggeredData {
	readonly webhookEventId: EventId;
	readonly source: string;
	readonly goalNodeId: number | null;
	readonly autonomyDomain: string;
	readonly effectiveTrust: TrustTier;
	readonly envelopeNonce: string;
}

export type ReflexOutcome =
	| { readonly kind: "noop"; readonly reason: string }
	| { readonly kind: "memory_written"; readonly nodeIds: readonly number[] }
	| { readonly kind: "proposal_requested"; readonly proposalId: string };

export interface ReflexThoughtData {
	readonly reflexEventId: EventId;
	readonly webhookEventId: EventId;
	readonly subagent: string;
	readonly model: string;
	readonly advisorModel?: string | undefined;
	readonly inputTokens: number;
	readonly outputTokens: number;
	readonly iterations: readonly IterationSummary[];
	readonly costUsd: number;
	readonly outcome: ReflexOutcome;
}

export interface ReflexSuppressedData {
	readonly webhookEventId: EventId;
	readonly reason: "stale" | "rate_limited" | "coalesced" | "no_match" | "degradation";
}

// ---------------------------------------------------------------------------
// Ideation events
// ---------------------------------------------------------------------------

export interface IdeationScheduledData {
	readonly runId: string;
	readonly kgCheckpoint: EventId | null;
	readonly sourceNodeIds: readonly number[];
	readonly model: string;
	readonly advisorModel?: string | undefined;
	readonly budgetCapUsd: number;
	readonly rngSeed: string;
}

export interface IdeationProposedData {
	readonly runId: string;
	readonly proposalText: string;
	readonly proposalHash: string;
	readonly referencedNodeIds: readonly number[];
	readonly confidence: number;
	readonly model: string;
	readonly advisorModel?: string | undefined;
	readonly iterations: readonly IterationSummary[];
	readonly costUsd: number;
}

export interface IdeationDuplicateSuppressedData {
	readonly runId: string;
	readonly proposalHash: string;
	readonly originalRunId: string;
}

export interface IdeationBudgetExceededData {
	readonly runId: string;
	readonly scope: "run" | "week" | "month";
	readonly capUsd: number;
	readonly spentUsd: number;
}

export interface IdeationBackoffExtendedData {
	readonly nextRunAt: string;
	readonly reason: "consecutive_rejections";
}

// ---------------------------------------------------------------------------
// Proposal events
// ---------------------------------------------------------------------------

export type ProposalOrigin = "ideation" | "reflex" | "owner_request" | "executive";

export type ProposalKind =
	| "new_goal"
	| "goal_plan"
	| "memory_write"
	| "code_change"
	| "message_draft"
	| "calendar_hold"
	| "workflow_change";

export interface ProposalRequestedData {
	readonly proposalId: string;
	readonly origin: ProposalOrigin;
	readonly kind: ProposalKind;
	readonly title: string;
	readonly summary: string;
	readonly payload: Record<string, unknown>;
	readonly autonomyDomain: string;
	readonly requiredLevel: number;
	readonly effectiveTrust: TrustTier;
	readonly expiresAt: string;
}

export interface ProposalDraftedData {
	readonly proposalId: string;
	readonly workspaceBranch?: string | undefined;
	readonly workspaceDraftId?: string | undefined;
}

export interface ProposalApprovedData {
	readonly proposalId: string;
	readonly approvedBy: Actor;
}

export interface ProposalRejectedData {
	readonly proposalId: string;
	readonly rejectedBy: Actor;
	readonly feedback?: string | undefined;
}

export interface ProposalExecutedData {
	readonly proposalId: string;
	readonly outcomeEventIds: readonly EventId[];
}

export interface ProposalExpiredData {
	readonly proposalId: string;
}

export interface ProposalRedactedData {
	readonly proposalId: string;
	readonly redactedBy: Actor;
}

// ---------------------------------------------------------------------------
// Egress events
// ---------------------------------------------------------------------------

export interface ConsentGrantedData {
	readonly policy: string;
	readonly scope?: string | undefined;
	readonly grantedBy: Actor;
	readonly reason?: string | undefined;
}

export interface ConsentRevokedData {
	readonly policy: string;
	readonly scope?: string | undefined;
	readonly revokedBy: Actor;
}

export interface EgressSensitivityUpdatedData {
	readonly dimension: string;
	readonly newSensitivity: "public" | "private" | "local_only";
	readonly setBy: Actor;
}

export interface CloudEgressTurnData {
	readonly subagent: string;
	readonly model: string;
	readonly advisorModel?: string | undefined;
	readonly inputTokens: number;
	readonly outputTokens: number;
	readonly costUsd: number;
	readonly dimensionsIncluded: readonly string[];
	readonly dimensionsExcluded: readonly string[];
	readonly turnClass: TurnClass;
	readonly causeEventId: EventId;
}

// ---------------------------------------------------------------------------
// Degradation events
// ---------------------------------------------------------------------------

export interface DegradationLevelChangedData {
	readonly previousLevel: number;
	readonly newLevel: number;
	readonly reason: string;
}

// ---------------------------------------------------------------------------
// Unions
// ---------------------------------------------------------------------------

export type WebhookEvent =
	| TheoEvent<"webhook.received", WebhookReceivedData>
	| TheoEvent<"webhook.verified", WebhookVerifiedData>
	| TheoEvent<"webhook.rejected", WebhookRejectedData>
	| TheoEvent<"webhook.rate_limited", WebhookRateLimitedData>
	| TheoEvent<"webhook.secret_rotated", WebhookSecretRotatedData>
	| TheoEvent<"webhook.secret_grace_expired", WebhookSecretGraceExpiredData>;

export type ReflexEvent =
	| TheoEvent<"reflex.triggered", ReflexTriggeredData>
	| TheoEvent<"reflex.thought", ReflexThoughtData>
	| TheoEvent<"reflex.suppressed", ReflexSuppressedData>;

export type IdeationEvent =
	| TheoEvent<"ideation.scheduled", IdeationScheduledData>
	| TheoEvent<"ideation.proposed", IdeationProposedData>
	| TheoEvent<"ideation.duplicate_suppressed", IdeationDuplicateSuppressedData>
	| TheoEvent<"ideation.budget_exceeded", IdeationBudgetExceededData>
	| TheoEvent<"ideation.backoff_extended", IdeationBackoffExtendedData>;

export type ProposalEvent =
	| TheoEvent<"proposal.requested", ProposalRequestedData>
	| TheoEvent<"proposal.drafted", ProposalDraftedData>
	| TheoEvent<"proposal.approved", ProposalApprovedData>
	| TheoEvent<"proposal.rejected", ProposalRejectedData>
	| TheoEvent<"proposal.executed", ProposalExecutedData>
	| TheoEvent<"proposal.expired", ProposalExpiredData>
	| TheoEvent<"proposal.redacted", ProposalRedactedData>;

export type EgressEvent =
	| TheoEvent<"policy.autonomous_cloud_egress.enabled", ConsentGrantedData>
	| TheoEvent<"policy.autonomous_cloud_egress.disabled", ConsentRevokedData>
	| TheoEvent<"policy.egress_sensitivity.updated", EgressSensitivityUpdatedData>
	| TheoEvent<"cloud_egress.turn", CloudEgressTurnData>;

export type DegradationEvent = TheoEvent<"degradation.level_changed", DegradationLevelChangedData>;
