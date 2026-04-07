# Phase 2: Event Types

## Motivation

The event type system is the spine of Theo's architecture. Every state change -- messages, memory
operations, scheduler actions, system lifecycle -- is modeled as a typed, immutable event. Defining
all event types upfront means every subsequent phase gets compile-time exhaustive checking: add a
new handler, and TypeScript forces you to handle every event variant. Miss one, and `tsc` fails.

This phase also establishes the upcaster registry for schema evolution. Events are immutable forever
-- when a schema changes, upcasters transform old shapes to new ones at read time. Getting this
right now means Theo can evolve for a decade without data migrations.

## Depends on

**Phase 1** -- Error types (`Result<T, E>`) and ULID dependency.

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/events/types.ts` | `TheoEvent<T, D>` interface, all event group unions, top-level `Event` union, `EphemeralEvent`, helper extraction types |
| `src/events/ids.ts` | Branded `EventId` type wrapping ULID, factory function |
| `src/events/upcasters.ts` | Upcaster registry: register, apply, chain walking, `CURRENT_VERSIONS` map |
| `tests/events/types.test.ts` | Type-level correctness tests |
| `tests/events/upcasters.test.ts` | Upcaster registration, chain execution, gap detection |

## Design Decisions

### Event Interface

```typescript
interface TheoEvent<T extends string = string, D = Record<string, unknown>> {
  readonly id: EventId;
  readonly type: T;
  readonly version: number;
  readonly timestamp: Date;
  readonly actor: Actor;
  readonly data: D;
  readonly metadata: EventMetadata;
}

type Actor = "user" | "theo" | "scheduler" | "system";

interface EventMetadata {
  readonly traceId?: string;
  readonly sessionId?: string;
  readonly causeId?: EventId;
  readonly gate?: string;
}
```

All fields `readonly`. The interface is generic over `T` (event type discriminant) and `D` (data
payload).

### Event Groups (Discriminated Unions)

Every data payload uses concrete types. No `unknown` fields.

```typescript
// --- Chat Events ---

interface MessageReceivedData {
  readonly body: string;
  readonly channel: string;
}

interface TurnStartedData {
  readonly sessionId: string;
}

interface TurnCompletedData {
  readonly responseBody: string;
  readonly durationMs: number;
  readonly tokensUsed: number;
}

interface TurnFailedData {
  readonly errorType: string;
  readonly message: string;
}

interface SessionCreatedData {
  readonly sessionId: string;
}

interface SessionReleasedData {
  readonly sessionId: string;
  readonly reason: string;
}

interface SessionCompactingData {
  readonly sessionId: string;
  readonly messageCount: number;
}

interface SessionCompactedData {
  readonly sessionId: string;
  readonly preservedTokens: number;
}

type ChatEvent =
  | TheoEvent<"message.received", MessageReceivedData>
  | TheoEvent<"turn.started", TurnStartedData>
  | TheoEvent<"turn.completed", TurnCompletedData>
  | TheoEvent<"turn.failed", TurnFailedData>
  | TheoEvent<"session.created", SessionCreatedData>
  | TheoEvent<"session.released", SessionReleasedData>
  | TheoEvent<"session.compacting", SessionCompactingData>
  | TheoEvent<"session.compacted", SessionCompactedData>;

// --- Memory Events ---

type NodeKind =
  | "fact" | "preference" | "observation" | "belief"
  | "goal" | "person" | "place" | "event"
  | "pattern" | "principle";
type Sensitivity = "none" | "sensitive" | "restricted";

interface NodeCreatedData {
  readonly nodeId: number;
  readonly kind: NodeKind;
  readonly body: string;
  readonly sensitivity: Sensitivity;
}

// Typed update shapes instead of `unknown` old/new values.
// Each variant describes a specific field mutation on a node.
type NodeUpdate =
  | { readonly field: "body"; readonly oldValue: string; readonly newValue: string }
  | { readonly field: "kind"; readonly oldValue: NodeKind; readonly newValue: NodeKind }
  | {
      readonly field: "sensitivity";
      readonly oldValue: Sensitivity;
      readonly newValue: Sensitivity;
    }
  | { readonly field: "confidence"; readonly oldValue: number; readonly newValue: number };

interface NodeUpdatedData {
  readonly nodeId: number;
  readonly update: NodeUpdate;
}

interface EdgeCreatedData {
  readonly edgeId: number;
  readonly sourceId: number;
  readonly targetId: number;
  readonly label: string;
  readonly weight: number;
}

interface EdgeExpiredData {
  readonly edgeId: number;
}

interface EpisodeCreatedData {
  readonly episodeId: number;
  readonly sessionId: string;
  readonly role: string;
}

interface CoreUpdatedData {
  readonly slot: string;
  readonly changedBy: Actor;
}

interface ContradictionDetectedData {
  readonly nodeId: number;
  readonly conflictId: number;
  readonly explanation: string;
}

interface UserModelUpdatedData {
  readonly dimension: string;
  readonly confidence: number;
}

interface SelfModelUpdatedData {
  readonly domain: string;
  readonly calibration: number;
}

interface SkillCreatedData {
  readonly skillId: number;
  readonly name: string;
  readonly trigger: string;
}

interface SkillPromotedData {
  readonly skillId: number;
  readonly promotedTo: "persona";
}

interface NodeDecayedData {
  readonly nodeCount: number;
  readonly minImportanceAfter: number;
}

interface PatternSynthesizedData {
  readonly patternNodeId: number;
  readonly sourceNodeIds: readonly number[];
  readonly kind: "pattern" | "principle";
}

interface NodeMergedData {
  readonly keptId: number;
  readonly mergedId: number;
}

interface NodeImportancePropagatedData {
  readonly nodeId: number;
  readonly boostDelta: number;
  readonly hopsTraversed: number;
}

interface NodeConfidenceAdjustedData {
  readonly nodeId: number;
  readonly delta: number;
  readonly newConfidence: number;
}

interface NodeAccessedData {
  readonly nodeIds: readonly number[];
}

type MemoryEvent =
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

// --- Scheduler Events ---

interface JobCreatedData {
  readonly jobId: string;
  readonly name: string;
  readonly cron: string | null;
}

interface JobTriggeredData {
  readonly jobId: string;
  readonly executionId: string;
}

interface JobCompletedData {
  readonly jobId: string;
  readonly executionId: string;
  readonly durationMs: number;
}

interface JobFailedData {
  readonly jobId: string;
  readonly executionId: string;
  readonly errorType: string;
  readonly message: string;
}

interface JobCancelledData {
  readonly jobId: string;
}

interface NotificationCreatedData {
  readonly source: string;
  readonly body: string;
}

type SchedulerEvent =
  | TheoEvent<"job.created", JobCreatedData>
  | TheoEvent<"job.triggered", JobTriggeredData>
  | TheoEvent<"job.completed", JobCompletedData>
  | TheoEvent<"job.failed", JobFailedData>
  | TheoEvent<"job.cancelled", JobCancelledData>
  | TheoEvent<"notification.created", NotificationCreatedData>;

// --- System Events ---

interface SystemStartedData {
  readonly version: string;
}

interface SystemStoppedData {
  readonly reason: string;
}

interface HandlerDeadLetteredData {
  readonly handlerId: string;
  readonly eventId: EventId;
  readonly attempts: number;
  readonly lastError: string;
}

interface HookFailedData {
  readonly hookEvent: string;
  readonly error: string;
}

type SystemEvent =
  | TheoEvent<"system.started", SystemStartedData>
  | TheoEvent<"system.stopped", SystemStoppedData>
  | TheoEvent<"system.rollback", { fromCommit: string; toCommit: string; reason: string }>
  | TheoEvent<"system.handler.dead_lettered", HandlerDeadLetteredData>
  | TheoEvent<"hook.failed", HookFailedData>;

// --- Full Union ---
// Every handler must handle every variant in its group.
type Event = ChatEvent | MemoryEvent | SchedulerEvent | SystemEvent;
```

### Helper Extraction Types

These types let handlers and projections work with specific event variants without repeating the
full union:

```typescript
// Extract the full event type for a given type string.
// Usage: EventOfType<"turn.completed"> resolves to TheoEvent<"turn.completed", TurnCompletedData>
type EventOfType<T extends Event["type"]> = Extract<Event, { readonly type: T }>;

// Extract just the data payload for a given type string.
// Usage: EventData<"turn.completed"> resolves to TurnCompletedData
type EventData<T extends Event["type"]> = EventOfType<T>["data"];
```

Example usage in a handler:

```typescript
function handleTurnCompleted(event: EventOfType<"turn.completed">): void {
  const data: EventData<"turn.completed"> = event.data;
  // data is TurnCompletedData — fully typed, no cast needed
  console.log(`Turn took ${data.durationMs}ms, used ${data.tokensUsed} tokens`);
}
```

### Growing the Event Union

Each phase that introduces new events adds them to the relevant sub-union (e.g., a `GateEvent`
sub-union in Phase 11). The top-level `Event` union is updated to include the new sub-union:

```typescript
type Event = ChatEvent | MemoryEvent | SchedulerEvent | SystemEvent | GateEvent;
```

Any existing `switch` on `Event["type"]` that uses an `assertNever` default will immediately fail
`tsc` until the new variants are handled. This is by design -- the compiler enforces exhaustive
handling across the entire codebase whenever the event catalog grows.

### EphemeralEvent (Separate Type)

```typescript
type EphemeralEvent =
  | {
      readonly type: "stream.chunk";
      readonly data: {
        readonly text: string;
        readonly sessionId: string;
      };
    }
  | { readonly type: "stream.done"; readonly data: { readonly sessionId: string } };
```

`EphemeralEvent` is NOT in the `Event` union. The type system prevents accidentally skipping
persistence for durable events. You literally cannot pass an `EphemeralEvent` to `bus.emit()` --
different type.

### Branded EventId

```typescript
type EventId = string & { readonly __brand: "EventId" };

function newEventId(): EventId {
  return ulid() as EventId;
  // This `as` cast is the single exception — branding requires it.
  // The ULID library returns `string`, and we brand it exactly once here.
}
```

Branded types prevent passing arbitrary strings where an EventId is expected.

### Upcaster Registry

The registry manages schema evolution. Each event type has a current version, and upcasters
transform old versions forward.

#### CURRENT_VERSIONS Map

```typescript
// Records the current schema version for each event type.
// Updated whenever a new upcaster is registered.
// `upcast()` uses this to know when to stop applying transforms.
type CurrentVersions = ReadonlyMap<string, number>;

// Initialized with version 1 for all event types defined in this phase.
// When no upcaster is registered for a type, it stays at version 1.
const CURRENT_VERSIONS: Map<string, number> = new Map([
  ["message.received", 1],
  ["turn.started", 1],
  ["turn.completed", 1],
  ["turn.failed", 1],
  ["session.created", 1],
  ["session.released", 1],
  ["session.compacting", 1],
  ["session.compacted", 1],
  ["memory.node.created", 1],
  ["memory.node.updated", 1],
  ["memory.edge.created", 1],
  ["memory.edge.expired", 1],
  ["memory.episode.created", 1],
  ["memory.core.updated", 1],
  ["memory.contradiction.detected", 1],
  ["memory.user_model.updated", 1],
  ["memory.self_model.updated", 1],
  ["job.created", 1],
  ["job.triggered", 1],
  ["job.completed", 1],
  ["job.failed", 1],
  ["job.cancelled", 1],
  ["notification.created", 1],
  ["system.started", 1],
  ["system.stopped", 1],
  ["system.rollback", 1],
  ["system.handler.dead_lettered", 1],
  ["memory.skill.created", 1],
  ["memory.skill.promoted", 1],
  ["memory.node.decayed", 1],
  ["memory.pattern.synthesized", 1],
  ["memory.node.merged", 1],
  ["memory.node.importance.propagated", 1],
  ["memory.node.confidence_adjusted", 1],
  ["memory.node.accessed", 1],
  ["hook.failed", 1],
  ["session.topic_continued", 1],
]);
```

#### Registry Interface

```typescript
type Upcaster = (data: Record<string, unknown>) => Record<string, unknown>;

interface UpcasterRegistry {
  /**
   * Register an upcaster that transforms data from `fromVersion` to `fromVersion + 1`.
   * Automatically updates CURRENT_VERSIONS to `fromVersion + 1` if that is higher
   * than the current recorded version.
   */
  register(eventType: string, fromVersion: number, fn: Upcaster): void;

  /**
   * Apply all upcasters in sequence from `fromVersion` up to the current version
   * recorded in CURRENT_VERSIONS for this event type.
   * If `fromVersion` equals the current version, returns data unchanged.
   */
  upcast(eventType: string, fromVersion: number, data: Record<string, unknown>): Record<string, unknown>;

  /**
   * Validate that all registered chains are contiguous (no gaps).
   * Call at startup before any event replay.
   * Returns a list of missing links, empty if all chains are valid.
   */
  validate(): ReadonlyArray<{ eventType: string; missingVersion: number }>;

  /** Read-only access to the current version map. */
  readonly currentVersions: CurrentVersions;
}
```

`upcast()` walks the chain: if an event is at version 1 and `CURRENT_VERSIONS` says the current
version is 3, it applies `1->2` then `2->3`. If `CURRENT_VERSIONS` has no entry for the event type
(unknown type), it returns data unchanged. A missing link in the chain is caught by `validate()` at
startup, before any replay begins.

When `register("turn.completed", 2, fn)` is called, `CURRENT_VERSIONS` for `"turn.completed"` is
updated to `3` (the target version of that upcaster).

## Definition of Done

- [ ] All event type unions compile with `tsc --noEmit`
- [ ] Exhaustive switch on `Event["type"]` requires all variants (verified by `assertNever`)
- [ ] `EphemeralEvent` is type-incompatible with `Event` (compile-time check)
- [ ] `EventId` brand prevents raw string assignment (compile-time check)
- [ ] `EventOfType<"turn.completed">` resolves to the correct event variant
- [ ] `EventData<"turn.completed">` resolves to `TurnCompletedData`
- [ ] No `unknown` in any event data payload -- every field has a concrete type
- [ ] `CURRENT_VERSIONS` is initialized with all event types at version 1
- [ ] `register()` updates `CURRENT_VERSIONS` to reflect the new target version
- [ ] Upcaster chains apply transforms in order (v1->v2->v3)
- [ ] Missing upcaster in chain is detected by `validate()` at startup
- [ ] `NodeKind` includes `"pattern"` and `"principle"` variants
- [ ] All new event types included in respective unions and `CURRENT_VERSIONS`
- [ ] `just check` passes

## Test Cases

### `tests/events/types.test.ts`

| Test | What it verifies |
| ------ | ----------------- |
| `assertNever` exhaustiveness | A function switching on `Event["type"]` covers all cases; adding a variant to the union without handling it fails `tsc` |
| EventId branding | `newEventId()` returns a string that satisfies `EventId`; a plain string does not assignable to `EventId` |
| Readonly enforcement | Event data cannot be mutated (compile-time -- verified by attempting assignment in test) |
| `EventOfType` extraction | `EventOfType<"memory.node.updated">` resolves to `TheoEvent<"memory.node.updated", NodeUpdatedData>` |
| `EventData` extraction | `EventData<"turn.failed">` resolves to `TurnFailedData` |
| `NodeUpdate` discriminant | Switching on `update.field` gives typed `oldValue`/`newValue` (e.g., `field: "body"` narrows both to `string`) |

### `tests/events/upcasters.test.ts`

| Test | Input | Expected |
| ------ | ------- | ---------- |
| Single upcaster | v1 data, registered 1->2 | v2 data with new field; `currentVersions.get(type)` is 2 |
| Chain upcaster | v1 data, registered 1->2 and 2->3 | v3 data with both transforms applied; `currentVersions.get(type)` is 3 |
| No upcaster needed | v3 data, current version 3 | Data returned unchanged |
| Missing chain link | Register 1->2 and 3->4 (skip 2->3) | `validate()` returns `[{ eventType, missingVersion: 2 }]` |
| Unknown event type | Unregistered type, v1 | Returns data as-is (no upcaster needed = assumed current) |
| CURRENT_VERSIONS init | No upcasters registered | All event types present in map at version 1 |
| Register updates version | Register upcaster for `turn.completed` from v1 | `currentVersions.get("turn.completed")` is 2 |

## Risks

**Low risk.** This is pure TypeScript -- no I/O, no database, no external services. The main
challenge is getting the event type definitions complete enough to avoid constant revision in later
phases, while accepting that some event payloads will be refined as their handlers are built.

The `NodeUpdate` union may need new variants as memory features are implemented (e.g., updating tags
or embeddings). Adding a variant to `NodeUpdate` is a non-breaking change -- existing switches get a
`tsc` error until they handle the new case, which is the desired behavior.
