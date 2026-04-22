/**
 * Event type system for Theo.
 *
 * Every state change -- messages, memory operations, scheduler actions, system lifecycle --
 * is modeled as a typed, immutable event. The Event union enables exhaustive switch/case
 * handling: miss a variant, and tsc fails.
 *
 * EphemeralEvent is a separate type that is NOT part of the Event union.
 * The type system prevents accidentally skipping persistence for durable events.
 */

import type { GoalEvent } from "./goals.ts";
import type { EventId } from "./ids.ts";
import type {
	DegradationEvent,
	EgressEvent,
	IdeationEvent,
	ProposalEvent,
	ReflexEvent,
	WebhookEvent,
} from "./reflexes.ts";

// ---------------------------------------------------------------------------
// Core Event Interface
// ---------------------------------------------------------------------------

/** Actor who caused the event. */
export type Actor = "user" | "theo" | "scheduler" | "system";

/** Role of a message in a conversation. */
export type MessageRole = "user" | "assistant";

/** The four named slots that comprise core memory. */
export type CoreMemorySlot = "persona" | "goals" | "user_model" | "context";

/** Optional metadata attached to every event. */
export interface EventMetadata {
	readonly traceId?: string | undefined;
	readonly sessionId?: string | undefined;
	readonly causeId?: EventId | undefined;
	readonly gate?: string | undefined;
	/**
	 * Effective trust tier propagated down a causation chain (foundation.md
	 * §7.3). When set, downstream handlers can enforce the tier at write
	 * boundaries. Stored as the TrustTier string to avoid a cross-module
	 * import at this base layer.
	 */
	readonly goalEffectiveTrust?: string | undefined;
}

/**
 * Base event interface. Generic over T (event type discriminant) and D (data payload).
 * All fields are readonly -- events are immutable.
 */
export interface TheoEvent<T extends string = string, D = Record<string, unknown>> {
	readonly id: EventId;
	readonly type: T;
	readonly version: number;
	readonly timestamp: Date;
	readonly actor: Actor;
	readonly data: D;
	readonly metadata: EventMetadata;
}

// ---------------------------------------------------------------------------
// Chat Events
// ---------------------------------------------------------------------------

export interface MessageReceivedData {
	readonly body: string;
	readonly channel: string;
}

export interface TurnStartedData {
	readonly sessionId: string;
	readonly prompt: string;
}

export interface TurnCompletedData {
	readonly sessionId: string;
	readonly responseBody: string;
	readonly durationMs: number;
	readonly inputTokens: number;
	readonly outputTokens: number;
	readonly totalTokens: number;
	readonly costUsd: number;
}

/**
 * Subtype of the SDK's terminal error result. Mirrors
 * `SDKResultError["subtype"]` so the event log stays canonical when the SDK
 * classifies a failed turn.
 */
export type TurnErrorType =
	| "error_during_execution"
	| "error_max_turns"
	| "error_max_budget_usd"
	| "error_max_structured_output_retries";

export interface TurnFailedData {
	readonly sessionId: string;
	readonly errorType: TurnErrorType;
	readonly errors: readonly string[];
	readonly durationMs: number;
}

export interface SessionCreatedData {
	readonly sessionId: string;
}

export interface SessionReleasedData {
	readonly sessionId: string;
	readonly reason: string;
}

/**
 * Trigger reported by the SDK's PreCompact/PostCompact hooks. `"manual"` comes
 * from `/compact`; `"auto"` from automatic context compaction.
 */
export type CompactTrigger = "manual" | "auto";

export interface SessionCompactingData {
	readonly sessionId: string;
	readonly trigger: CompactTrigger;
}

export interface SessionCompactedData {
	readonly sessionId: string;
	readonly summary: string;
}

export type ChatEvent =
	| TheoEvent<"message.received", MessageReceivedData>
	| TheoEvent<"turn.started", TurnStartedData>
	| TheoEvent<"turn.completed", TurnCompletedData>
	| TheoEvent<"turn.failed", TurnFailedData>
	| TheoEvent<"session.created", SessionCreatedData>
	| TheoEvent<"session.released", SessionReleasedData>
	| TheoEvent<"session.compacting", SessionCompactingData>
	| TheoEvent<"session.compacted", SessionCompactedData>;

// ---------------------------------------------------------------------------
// Memory Events
// ---------------------------------------------------------------------------

export type NodeKind =
	| "fact"
	| "preference"
	| "observation"
	| "belief"
	| "goal"
	| "person"
	| "place"
	| "event"
	| "pattern"
	| "principle";

/** Severity level for privacy-sensitive memory nodes. */
export type Sensitivity = "none" | "sensitive" | "restricted";

export interface NodeCreatedData {
	readonly nodeId: number;
	readonly kind: NodeKind;
	readonly body: string;
	readonly sensitivity: Sensitivity;
	readonly hasEmbedding: boolean;
}

/**
 * Typed update shapes for node mutations.
 * Each variant describes a specific field mutation, giving typed oldValue/newValue pairs.
 * Discriminated on `field`.
 */
export type NodeUpdate =
	| { readonly field: "body"; readonly oldValue: string; readonly newValue: string }
	| { readonly field: "kind"; readonly oldValue: NodeKind; readonly newValue: NodeKind }
	| {
			readonly field: "sensitivity";
			readonly oldValue: Sensitivity;
			readonly newValue: Sensitivity;
	  }
	| { readonly field: "confidence"; readonly oldValue: number; readonly newValue: number };

export interface NodeUpdatedData {
	readonly nodeId: number;
	readonly update: NodeUpdate;
}

export interface EdgeCreatedData {
	readonly edgeId: number;
	readonly sourceId: number;
	readonly targetId: number;
	readonly label: string;
	readonly weight: number;
}

export interface EdgeExpiredData {
	readonly edgeId: number;
}

export interface EpisodeCreatedData {
	readonly episodeId: number;
	readonly sessionId: string;
	readonly role: MessageRole;
}

export interface CoreUpdatedData {
	readonly slot: CoreMemorySlot;
	readonly changedBy: Actor;
}

export interface ContradictionDetectedData {
	readonly nodeId: number;
	readonly conflictId: number;
	readonly explanation: string;
}

/**
 * Decision event: classification was requested. Stores the candidate pair and
 * acts as the deterministic anchor for the effect handler. On replay, a
 * decision handler sees this event but the actual LLM call is skipped.
 */
export interface ContradictionRequestedData {
	readonly nodeId: number;
	readonly candidateId: number;
}

/**
 * Effect event: the LLM classifier answered. Written by the effect handler
 * after the classification call succeeds (or fails — `contradicts: false` is
 * emitted for failure paths). Downstream decision handlers read this event to
 * adjust confidence and create the contradicts edge.
 */
export interface ContradictionClassifiedData {
	readonly nodeId: number;
	readonly candidateId: number;
	readonly contradicts: boolean;
	readonly explanation: string;
}

/** Decision event: summarization of one session's episodes was requested. */
export interface EpisodeSummarizeRequestedData {
	readonly sessionId: string;
	readonly episodeIds: readonly number[];
}

/** Effect event: the summarizer answered. The consolidation decision handler
 *  persists the summary episode and updates `superseded_by`. */
export interface EpisodeSummarizedData {
	readonly sessionId: string;
	readonly episodeIds: readonly number[];
	readonly summary: string;
}

export interface UserModelUpdatedData {
	readonly dimension: string;
	readonly confidence: number;
}

export interface SelfModelUpdatedData {
	readonly domain: string;
	readonly calibration: number;
	readonly correct?: boolean | undefined;
}

export interface SkillCreatedData {
	readonly skillId: number;
	readonly name: string;
	readonly trigger: string;
}

export interface SkillPromotedData {
	readonly skillId: number;
	readonly promotedTo: "persona";
}

export interface NodeDecayedData {
	readonly nodeCount: number;
	readonly minImportanceAfter: number;
}

export interface PatternSynthesizedData {
	readonly patternNodeId: number;
	readonly sourceNodeIds: readonly number[];
	readonly kind: "pattern" | "principle";
}

export interface NodeMergedData {
	readonly keptId: number;
	readonly mergedId: number;
}

export interface NodeImportancePropagatedData {
	readonly nodeId: number;
	readonly boostDelta: number;
	readonly hopsTraversed: number;
}

export interface NodeConfidenceAdjustedData {
	readonly nodeId: number;
	readonly delta: number;
	readonly newConfidence: number;
}

export interface NodeAccessedData {
	readonly nodeIds: readonly number[];
}

export type MemoryEvent =
	| TheoEvent<"memory.node.created", NodeCreatedData>
	| TheoEvent<"memory.node.updated", NodeUpdatedData>
	| TheoEvent<"memory.edge.created", EdgeCreatedData>
	| TheoEvent<"memory.edge.expired", EdgeExpiredData>
	| TheoEvent<"memory.episode.created", EpisodeCreatedData>
	| TheoEvent<"memory.core.updated", CoreUpdatedData>
	| TheoEvent<"memory.contradiction.detected", ContradictionDetectedData>
	| TheoEvent<"contradiction.requested", ContradictionRequestedData>
	| TheoEvent<"contradiction.classified", ContradictionClassifiedData>
	| TheoEvent<"episode.summarize_requested", EpisodeSummarizeRequestedData>
	| TheoEvent<"episode.summarized", EpisodeSummarizedData>
	| TheoEvent<"memory.user_model.updated", UserModelUpdatedData>
	| TheoEvent<"memory.self_model.updated", SelfModelUpdatedData>
	| TheoEvent<"memory.skill.created", SkillCreatedData>
	| TheoEvent<"memory.skill.promoted", SkillPromotedData>
	| TheoEvent<"memory.node.decayed", NodeDecayedData>
	| TheoEvent<"memory.pattern.synthesized", PatternSynthesizedData>
	| TheoEvent<"memory.node.merged", NodeMergedData>
	| TheoEvent<"memory.node.importance.propagated", NodeImportancePropagatedData>
	| TheoEvent<"memory.node.confidence_adjusted", NodeConfidenceAdjustedData>
	| TheoEvent<"memory.node.accessed", NodeAccessedData>;

// ---------------------------------------------------------------------------
// Scheduler Events
// ---------------------------------------------------------------------------

export interface JobCreatedData {
	readonly jobId: string;
	readonly name: string;
	readonly cron: string | null;
}

export interface JobTriggeredData {
	readonly jobId: string;
	readonly jobName: string;
	readonly executionId: string;
}

export interface JobCompletedData {
	readonly jobId: string;
	readonly jobName: string;
	readonly executionId: string;
	readonly durationMs: number;
	readonly summary: string;
	readonly tokensUsed: number | null;
	readonly costUsd: number | null;
}

export interface JobFailedData {
	readonly jobId: string;
	readonly jobName: string;
	readonly executionId: string;
	readonly durationMs: number;
	readonly errorType: TurnErrorType;
	readonly message: string;
}

export interface JobCancelledData {
	readonly jobId: string;
	readonly jobName: string;
}

export interface NotificationCreatedData {
	readonly source: string;
	readonly body: string;
}

export type SchedulerEvent =
	| TheoEvent<"job.created", JobCreatedData>
	| TheoEvent<"job.triggered", JobTriggeredData>
	| TheoEvent<"job.completed", JobCompletedData>
	| TheoEvent<"job.failed", JobFailedData>
	| TheoEvent<"job.cancelled", JobCancelledData>
	| TheoEvent<"notification.created", NotificationCreatedData>;

// ---------------------------------------------------------------------------
// System Events
// ---------------------------------------------------------------------------

export interface SystemStartedData {
	readonly version: string;
}

export interface SystemStoppedData {
	readonly reason: string;
}

export interface SystemRollbackData {
	readonly fromCommit: string;
	readonly toCommit: string;
	readonly reason: string;
}

/**
 * Degradation ladder healing — the autonomic self-restoration counterpart
 * to `degradation.level_changed`. Emitted when the healing timer (Phase 15)
 * observes that the conditions that forced a degradation have cleared for
 * a sustained window.
 */
export interface SystemDegradationHealedData {
	readonly previousLevel: number;
	readonly newLevel: number;
	readonly reason: string;
}

/**
 * Emitted when the self-update path refuses to auto-merge because a SLO's
 * error budget is exhausted. The bot opens/holds the PR; no merge occurs.
 */
export interface SelfUpdateBlockedData {
	readonly slo: string;
	readonly budgetRemainingRatio: number;
	readonly reason: string;
}

/**
 * Result of one synthetic probe turn — the "canary" self-test Theo issues
 * on a schedule to detect alive-but-stuck failures that `launchd` misses.
 */
export interface SyntheticProbeCompletedData {
	readonly probeId: string;
	readonly ok: boolean;
	readonly durationMs: number;
	readonly reason?: string | undefined;
}

export interface HandlerDeadLetteredData {
	readonly handlerId: string;
	readonly eventId: EventId;
	readonly attempts: number;
	readonly lastError: string;
}

export interface HookFailedData {
	readonly hookEvent: string;
	readonly error: string;
}

export type SystemEvent =
	| TheoEvent<"system.started", SystemStartedData>
	| TheoEvent<"system.stopped", SystemStoppedData>
	| TheoEvent<"system.rollback", SystemRollbackData>
	| TheoEvent<"system.degradation.healed", SystemDegradationHealedData>
	| TheoEvent<"self_update.blocked", SelfUpdateBlockedData>
	| TheoEvent<"synthetic.probe.completed", SyntheticProbeCompletedData>
	| TheoEvent<"system.handler.dead_lettered", HandlerDeadLetteredData>
	| TheoEvent<"hook.failed", HookFailedData>;

// ---------------------------------------------------------------------------
// Full Event Union
// ---------------------------------------------------------------------------

/** Every handler must handle every variant in its group. */
export type Event =
	| ChatEvent
	| MemoryEvent
	| SchedulerEvent
	| SystemEvent
	| GoalEvent
	| WebhookEvent
	| ReflexEvent
	| IdeationEvent
	| ProposalEvent
	| EgressEvent
	| DegradationEvent;

export type { GoalEvent } from "./goals.ts";
export type {
	DegradationEvent,
	EgressEvent,
	IdeationEvent,
	ProposalEvent,
	ReflexEvent,
	WebhookEvent,
} from "./reflexes.ts";

// ---------------------------------------------------------------------------
// Helper Extraction Types
// ---------------------------------------------------------------------------

/**
 * Extract the full event type for a given type string.
 * Usage: EventOfType<"turn.completed"> resolves to TheoEvent<"turn.completed", TurnCompletedData>
 */
export type EventOfType<T extends Event["type"]> = Extract<Event, { readonly type: T }>;

/**
 * Extract just the data payload for a given type string.
 * Usage: EventData<"turn.completed"> resolves to TurnCompletedData
 */
export type EventData<T extends Event["type"]> = EventOfType<T>["data"];

// ---------------------------------------------------------------------------
// Ephemeral Events (NOT in the Event union)
// ---------------------------------------------------------------------------

/**
 * Ephemeral events are not persisted to the event log.
 * The type system prevents accidentally passing an EphemeralEvent where an Event is expected.
 */
export type EphemeralEvent =
	| {
			readonly type: "stream.chunk";
			readonly data: { readonly text: string; readonly sessionId: string };
	  }
	| { readonly type: "stream.done"; readonly data: { readonly sessionId: string } }
	| {
			readonly type: "tool.start";
			readonly data: {
				readonly name: string;
				readonly input: string;
				readonly callId: string;
				readonly sessionId: string;
			};
	  }
	| {
			readonly type: "tool.done";
			readonly data: {
				readonly callId: string;
				readonly durationMs: number;
				readonly sessionId: string;
			};
	  };

// ---------------------------------------------------------------------------
// Exhaustive Switch Helper
// ---------------------------------------------------------------------------

/**
 * Helper for exhaustive switch/case on discriminated unions.
 * Pass the default case value to assertNever -- if it's reachable, tsc fails.
 */
export function assertNever(value: never): never {
	throw new Error(`Unexpected value: ${String(value)}`);
}
