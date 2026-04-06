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

import type { EventId } from "./ids.ts";

// ---------------------------------------------------------------------------
// Core Event Interface
// ---------------------------------------------------------------------------

/** Actor who caused the event. */
export type Actor = "user" | "theo" | "scheduler" | "system";

/** Optional metadata attached to every event. */
export interface EventMetadata {
	readonly traceId?: string | undefined;
	readonly sessionId?: string | undefined;
	readonly causeId?: EventId | undefined;
	readonly gate?: string | undefined;
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
}

export interface TurnCompletedData {
	readonly responseBody: string;
	readonly durationMs: number;
	readonly tokensUsed: number;
}

export interface TurnFailedData {
	readonly errorType: string;
	readonly message: string;
}

export interface SessionCreatedData {
	readonly sessionId: string;
}

export interface SessionReleasedData {
	readonly sessionId: string;
	readonly reason: string;
}

export interface SessionCompactingData {
	readonly sessionId: string;
	readonly messageCount: number;
}

export interface SessionCompactedData {
	readonly sessionId: string;
	readonly preservedTokens: number;
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

export type Sensitivity =
	| "normal"
	| "financial"
	| "medical"
	| "identity"
	| "location"
	| "relationship";

export interface NodeCreatedData {
	readonly nodeId: number;
	readonly kind: NodeKind;
	readonly body: string;
	readonly sensitivity: Sensitivity;
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
	readonly role: string;
}

export interface CoreUpdatedData {
	readonly slot: string;
	readonly changedBy: Actor;
}

export interface ContradictionDetectedData {
	readonly nodeId: number;
	readonly conflictId: number;
	readonly explanation: string;
}

export interface UserModelUpdatedData {
	readonly dimension: string;
	readonly confidence: number;
}

export interface SelfModelUpdatedData {
	readonly domain: string;
	readonly calibration: number;
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
	readonly executionId: string;
}

export interface JobCompletedData {
	readonly jobId: string;
	readonly executionId: string;
	readonly durationMs: number;
}

export interface JobFailedData {
	readonly jobId: string;
	readonly executionId: string;
	readonly errorType: string;
	readonly message: string;
}

export interface JobCancelledData {
	readonly jobId: string;
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
	| TheoEvent<"system.handler.dead_lettered", HandlerDeadLetteredData>
	| TheoEvent<"hook.failed", HookFailedData>;

// ---------------------------------------------------------------------------
// Full Event Union
// ---------------------------------------------------------------------------

/** Every handler must handle every variant in its group. */
export type Event = ChatEvent | MemoryEvent | SchedulerEvent | SystemEvent;

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
	| { readonly type: "stream.done"; readonly data: { readonly sessionId: string } };

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

// ---------------------------------------------------------------------------
// All Event Types (for CURRENT_VERSIONS initialization)
// ---------------------------------------------------------------------------

/**
 * Array of all event type strings, used to initialize CURRENT_VERSIONS in the upcaster registry.
 * This single source of truth prevents the version map from drifting out of sync with the union.
 */
export const ALL_EVENT_TYPES = [
	// Chat
	"message.received",
	"turn.started",
	"turn.completed",
	"turn.failed",
	"session.created",
	"session.released",
	"session.compacting",
	"session.compacted",
	// Memory
	"memory.node.created",
	"memory.node.updated",
	"memory.edge.created",
	"memory.edge.expired",
	"memory.episode.created",
	"memory.core.updated",
	"memory.contradiction.detected",
	"memory.user_model.updated",
	"memory.self_model.updated",
	"memory.skill.created",
	"memory.skill.promoted",
	"memory.node.decayed",
	"memory.pattern.synthesized",
	"memory.node.merged",
	"memory.node.importance.propagated",
	"memory.node.confidence_adjusted",
	"memory.node.accessed",
	// Scheduler
	"job.created",
	"job.triggered",
	"job.completed",
	"job.failed",
	"job.cancelled",
	"notification.created",
	// System
	"system.started",
	"system.stopped",
	"system.rollback",
	"system.handler.dead_lettered",
	"hook.failed",
	// Future (reserved in CURRENT_VERSIONS per plan)
	"session.topic_continued",
] as const satisfies readonly string[];
