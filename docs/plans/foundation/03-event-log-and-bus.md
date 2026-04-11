# Phase 3: Event Log & Bus

> **Status: SHIPPED.** This phase is implemented. Two post-foundation amendments are
> planned:
>
> 1. **Handler mode flag (`decision` | `effect`)** — added in Phase 13's scope to let
>    replay skip side-effecting handlers. See `foundation.md §7.4` and
>    `docs/plans/foundation/13-background-intelligence.md` for the amendment.
> 2. **`effective_trust_tier` column on events** — added in Phase 13b's migration to
>    store the causation-chain effective trust at write time. See `foundation.md §7.3`
>    and `docs/plans/foundation/13b-ideation-and-reflexes.md §10` for the amendment.
>
> Both amendments are **additive** and do not break the existing bus contract.

## Motivation

The event log is Theo's primary record — the single source of truth from which all other state is
derived. The event bus unifies durability and dispatch: every `emit()` writes to PostgreSQL and then
dispatches to in-memory handlers. Together they form the nervous system that coordinates all of
Theo's subsystems.

Without this phase, nothing can react to anything. Memory can't update projections. Hooks can't
persist lifecycle events. The scheduler can't record job outcomes. Every subsequent phase depends on
being able to emit events and have handlers process them.

## Depends on

- **Phase 1** — DB pool, migration runner, error types
- **Phase 2** — Event types, EventId, upcaster registry

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/events/log.ts` | `EventLog` — append events to PostgreSQL, read with upcasting, partition management |
| `src/events/bus.ts` | `EventBus` — emit (write + dispatch), handler registration, start/stop, replay |
| `src/events/handlers.ts` | Handler type, retry loop, dead-letter logic |
| `src/events/queue.ts` | `HandlerQueue` — per-handler serialized queue with replay/live sub-queues |
| `src/db/migrations/0002_event_log.sql` | Events table (partitioned), handler_cursors, event_snapshots |
| `tests/events/log.test.ts` | Append/read roundtrip, upcaster application on read, partition creation |
| `tests/events/bus.test.ts` | Emit+dispatch, checkpointing, replay, dead-lettering, handler isolation, tx support |

## Design Decisions

### Migration: `0002_event_log.sql`

```sql
-- Partitioned events table
CREATE TABLE IF NOT EXISTS events (
  id         text        NOT NULL,  -- ULID
  type       text        NOT NULL,
  version    integer     NOT NULL DEFAULT 1,
  timestamp  timestamptz NOT NULL DEFAULT now(),
  actor      text        NOT NULL,
  data       jsonb       NOT NULL DEFAULT '{}',
  metadata   jsonb       NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id, timestamp)     -- must include partition key
) PARTITION BY RANGE (timestamp);

-- Handler checkpoint cursors
CREATE TABLE IF NOT EXISTS handler_cursors (
  handler_id text        PRIMARY KEY,
  cursor     text        NOT NULL,  -- ULID of last processed event
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Projection snapshots (for future Phase 13 consolidation)
CREATE TABLE IF NOT EXISTS event_snapshots (
  projection text        PRIMARY KEY,
  state      jsonb       NOT NULL,
  cursor     text        NOT NULL,  -- ULID of last event included
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
```

The migration does NOT create any partitions. Partition creation is handled by the `EventLog` at
runtime (see below).

### Partition Management

Partitions are named `events_YYYY_MM` and cover one calendar month each. The `EventLog` maintains a
`Set<string>` of known partition names, populated on startup and updated lazily on writes.

**Partition name computation:**

```typescript
function partitionName(timestamp: Date): string {
  const y = timestamp.getUTCFullYear();
  const m = String(timestamp.getUTCMonth() + 1).padStart(2, "0");
  return `events_${y}_${m}`;
}

function partitionBounds(timestamp: Date): { from: Date; to: Date } {
  const y = timestamp.getUTCFullYear();
  const m = timestamp.getUTCMonth();
  return {
    from: new Date(Date.UTC(y, m, 1)),
    to: new Date(Date.UTC(y, m + 1, 1)), // JS Date handles month overflow
  };
}
```

**Startup — populate known partitions:**

```typescript
async loadKnownPartitions(): Promise<void> {
  const rows = await sql`
    SELECT c.relname AS name
    FROM pg_catalog.pg_inherits i
    JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid
    JOIN pg_catalog.pg_class p ON p.oid = i.inhparent
    WHERE p.relname = 'events'
  `;
  this.knownPartitions = new Set(rows.map(r => r.name));
}
```

**Ensure partition exists — called from `append()`:**

```typescript
async ensurePartition(timestamp: Date): Promise<void> {
  const name = partitionName(timestamp);
  if (this.knownPartitions.has(name)) return;

  const { from, to } = partitionBounds(timestamp);
  await sql`
    CREATE TABLE IF NOT EXISTS ${sql(name)}
    PARTITION OF events
    FOR VALUES FROM (${from.toISOString()}) TO (${to.toISOString()})
  `;
  this.knownPartitions.add(name);
}
```

`CREATE TABLE IF NOT EXISTS` makes this idempotent under concurrency.

**Proactive partition creation on `start()`:**

```typescript
// Create partitions for current month and next month
const now = new Date();
await this.ensurePartition(now);
await this.ensurePartition(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1)));
```

This prevents the first write of a new month from paying the partition creation cost.

### EventLog

```typescript
interface EventLog {
  append(event: Omit<Event, "id" | "timestamp">, tx?: Transaction): Promise<Event>;
  read(options?: ReadOptions): AsyncGenerator<Event>;
  readAfter(cursor: EventId, options?: ReadOptions): AsyncGenerator<Event>;
  loadKnownPartitions(): Promise<void>;
}

interface ReadOptions {
  types?: ReadonlyArray<Event["type"]>;
  limit?: number;
  tx?: Transaction;  // use caller's transaction (for REPEATABLE READ replay)
}
```

- `append()` assigns ULID + timestamp, calls `ensurePartition()`, INSERTs (using `tx` if provided,
  otherwise the pool), returns the complete event.
- `read()` returns an async generator that yields events in ULID order (which is time order),
  applying upcasters lazily.
- `readAfter()` reads events after a specific cursor (for checkpoint replay).
- The `tx` parameter allows callers to include the event INSERT in an external transaction (see
  "Transaction Boundary" below).

### EventBus

```typescript
interface HandlerOptions {
  readonly id: string; // stable handler ID for checkpointing and replay
}

interface EventBus {
  on<T extends Event["type"]>(
    type: T,
    handler: Handler<Extract<Event, { type: T }>>,
    options?: HandlerOptions,
  ): void;

  emit(event: Omit<Event, "id" | "timestamp">, options?: { tx?: Transaction }): Promise<Event>;

  start(): Promise<void>;  // replay from checkpoints, then switch to live
  stop(): Promise<void>;   // finish current event per handler, discard queues
  flush(): Promise<void>;  // drain all handler queues completely (testing only)
}

// Durable handlers receive a transaction for atomic handler+checkpoint writes.
// Ephemeral handlers do not receive a transaction.
type Handler<E extends Event> = (event: E, tx?: Transaction) => Promise<void>;
```

**Durable vs ephemeral handlers.** Handlers registered with an `id` option are durable: they get a
checkpoint in `handler_cursors`, replay missed events on `start()`, and survive restarts. Handlers
registered without `id` are ephemeral: they receive only live events after registration, have no
checkpoint, and no replay. Ephemeral handlers are for non-critical side effects like logging and
metrics. Any handler that affects durable state MUST have a stable id.

### Transaction Boundary and `emit()` API Contract

`emit()` guarantees durability: it writes the event to PostgreSQL and returns the persisted event.
It does NOT wait for handler processing. Handlers are notified via synchronous queue enqueue after
the write succeeds, and process the event asynchronously via their drain loops.

```typescript
async emit(
  event: Omit<Event, "id" | "timestamp">,
  options?: { tx?: Transaction },
): Promise<Event> {
  // 1. Write to PostgreSQL — committed on return (or when caller's tx commits)
  const persisted = await this.log.append(event, options?.tx);

  // 2. Enqueue to matching handler queues — synchronous, no await
  this.enqueueToMatchingHandlers(persisted);

  return persisted;
}
```

**Why `emit()` does not await handlers:** The old design awaited `dispatch()` which awaited all
handlers via `Promise.allSettled`. This creates a deadlock when a handler emits during its own
processing (handler A processes event → emits new event → `emit()` awaits handler A → handler A
is already processing → deadlock). With queue enqueue, the new event enters A's queue and is
processed after the current event completes.

**Caller impact:** Code that previously relied on handlers finishing before `emit()` returns must
now use `bus.flush()` to wait for handler completion. This only affects tests. Production code
should not depend on handler completion timing — events are durable in PostgreSQL regardless.

If the INSERT fails (constraint violation, partition missing, connection error), no enqueue happens
— no phantom events. If a handler later fails processing the event, the event still exists in
PostgreSQL. The handler retries or dead-letters, but the event is never lost.

### Transaction Parameter for Atomic Event+Projection Writes

For handlers that need atomic writes (e.g., a repository inserting a projection row AND the event in
the same transaction), `emit()` accepts an optional `tx` parameter:

```typescript
await sql.begin(async (tx) => {
  await tx`INSERT INTO node (kind, body) VALUES (${kind}, ${body}) RETURNING id`;
  await bus.emit({ type: "memory.node.created", ... }, { tx });
});
```

When `tx` is provided, the event INSERT uses the transaction instead of the pool. The event is
committed when the caller's transaction commits. Dispatch still fires after the INSERT (within the
same async flow), but the event is only visible to other connections after commit.

This is the standard pattern for ensuring event+projection atomicity. Without `tx`, the event and
projection are separate writes — acceptable for non-critical projections, but not for operations
where a projection without its event (or vice versa) would be inconsistent.

### Handler Dispatch

Dispatch is a synchronous enqueue into per-handler queues. Each durable handler has its own
`HandlerQueue` (see next section). Ephemeral handlers are called inline since they have no
ordering or checkpoint requirements.

```typescript
private enqueueToMatchingHandlers(event: Event): void {
  for (const registration of this.handlers) {
    if (registration.type !== event.type) continue;

    if (registration.id === undefined) {
      // Ephemeral handler: fire-and-forget, no queue
      void registration.handler(event).catch(() => {
        // Log but do not retry — ephemeral handlers are best-effort
      });
      continue;
    }

    // Durable handler: enqueue for serial processing
    registration.queue.enqueueLive(event);
  }
}
```

One handler's queue never blocks another's — they drain independently.

### HandlerQueue Design (`src/events/queue.ts`)

Each durable handler gets a `HandlerQueue` that enforces the core invariant: **events are processed
one at a time, in ULID order, with monotonic checkpoint advancement.**

```typescript
class HandlerQueue {
  private replayQueue: Event[] = [];
  private liveQueue: Event[] = [];
  private draining = false;
  private stopped = false;
  private lastProcessedId: EventId | null = null;
  private wakeResolver: (() => void) | null = null;
  private drainedResolver: (() => void) | null = null;

  enqueueReplay(event: Event): void;   // fed from database replay cursor
  enqueueLive(event: Event): void;     // fed from emit() dispatch
  startDraining(): void;               // begins the drain loop
  stop(): void;                        // finish current, discard rest
  drained(): Promise<void>;            // resolves when both queues are empty
}
```

**Two sub-queues, replay-first drain.** Replay events have strictly lower ULIDs than live events
(guaranteed by MVCC snapshot — the replay query cannot see events emitted after it started).
Draining replay first preserves global ULID order without sorting.

**Drain loop pseudocode:**

```typescript
private async drain(handler, handlerId, advanceCursor, sql): Promise<void> {
  this.draining = true;
  while (!this.stopped) {
    // 1. Dequeue: replay first, then live
    const event = this.replayQueue.shift() ?? this.liveQueue.shift();

    if (!event) {
      // Queue empty — resolve drained promise if anyone is waiting
      this.drainedResolver?.();
      // Park until woken by enqueueReplay/enqueueLive or stop
      await new Promise<void>(r => { this.wakeResolver = r; });
      continue;
    }

    // 2. ULID dedup: skip events at or before the last processed
    if (this.lastProcessedId && event.id <= this.lastProcessedId) continue;

    // 3. Execute handler + checkpoint atomically
    await this.executeWithRetry(handler, handlerId, event, advanceCursor, sql);

    // 4. Track progress
    this.lastProcessedId = event.id;
  }
  this.draining = false;
}
```

**Re-entrancy prevention.** The `draining` flag ensures only one drain loop runs per queue. The
JS event loop is single-threaded: `await handler(event, tx)` runs to completion before the next
dequeue. No true parallelism within a handler.

**Wake mechanism.** When both queues are empty, the drain loop parks on a `Promise` whose resolver
is stored as `wakeResolver`. `enqueueLive()` and `enqueueReplay()` call `wakeResolver?.()` after
pushing to their respective queues, unblocking the loop.

**ULID dedup.** The MVCC timing window can produce overlapping events between the replay query's
snapshot boundary and live dispatch. The `event.id <= lastProcessedId` guard catches these. ULID
comparison is lexicographic — string comparison works for time ordering.

**`drained()` for testing.** Returns a `Promise` that resolves when both queues are empty and the
current event (if any) has finished processing. Used by `bus.flush()`.

### Retry Strategy

Failed handlers are retried immediately on the SAME event. No delays between retries — these are
in-memory operations, not network calls. A transient failure (e.g., a race condition, a temporary
resource issue) is resolved by immediate retry. A permanent failure (e.g., a bug in the handler)
fails fast after 3 attempts.

```typescript
private readonly MAX_RETRIES = 3;

// Called from within the HandlerQueue drain loop (already serialized per handler).
// Ephemeral handlers are not routed through queues — they are fire-and-forget in dispatch.
async executeWithRetry(
  handler: Handler<Event>,
  handlerId: string,
  event: Event,
  advanceCursor: (id: string, cursor: EventId, tx: Transaction) => Promise<void>,
  sql: Sql,
): Promise<void> {
  for (let attempt = 1; attempt <= this.MAX_RETRIES; attempt++) {
    try {
      // Atomic: handler side effects + checkpoint in one transaction
      await sql.begin(async (tx) => {
        await handler(event, tx);
        await advanceCursor(handlerId, event.id, tx);
      });
      return;
    } catch (error) {
      if (attempt === this.MAX_RETRIES) {
        // Dead-letter: record failure + advance cursor atomically
        await sql.begin(async (tx) => {
          await this.log.append(
            {
              type: "system.handler.dead_lettered",
              version: 1,
              actor: "system",
              data: {
                handlerId,
                eventId: event.id,
                attempts: this.MAX_RETRIES,
                lastError: error instanceof Error ? error.message : String(error),
              },
              metadata: {},
            },
            tx,
          );
          await advanceCursor(handlerId, event.id, tx);
        });
        return;
      }
      // Otherwise: loop immediately to next attempt
    }
  }
}
```

The dead-letter meta-event is appended to the log via `this.log.append()` within the same
transaction as the cursor advance. If the transaction fails, neither commits — on restart the
handler re-encounters the event, re-fails, and re-attempts dead-lettering. The dead-letter event
itself enters the bus via `enqueueToMatchingHandlers()` after the transaction commits (triggered
by the `log.append()` call which feeds back through `emit()`).

**Why no backoff:** Theo is a single-user, single-machine system. Handlers are in-memory functions
operating on local state. There is no remote service to back off from. If a handler fails 3 times in
a row, it has a bug — backing off won't help.

### Checkpointing

Each durable handler (registered with a stable `id`) gets a cursor in `handler_cursors`. The cursor
holds the ULID of the last event successfully processed by that handler.

```typescript
async advanceCursor(handlerId: string, eventId: EventId, tx: Transaction): Promise<void> {
  await tx`
    INSERT INTO handler_cursors (handler_id, cursor, updated_at)
    VALUES (${handlerId}, ${eventId}, now())
    ON CONFLICT (handler_id)
    DO UPDATE SET cursor = ${eventId}, updated_at = now()
    WHERE handler_cursors.cursor < ${eventId}
  `;
}

async getCursor(handlerId: string): Promise<EventId | null> {
  const rows = await sql`
    SELECT cursor FROM handler_cursors WHERE handler_id = ${handlerId}
  `;
  return rows[0]?.cursor ?? null;
}
```

**Monotonic guard.** The `WHERE handler_cursors.cursor < ${eventId}` clause ensures the cursor
never regresses, even if a stale `advanceCursor` call arrives out of order. Combined with the
per-handler queue serialization, this is a defense-in-depth measure.

**Transaction parameter.** `advanceCursor` now requires a `tx` parameter — it is always called
inside the same transaction as the handler's side effects (see Retry Strategy). This eliminates
the crash window between "handler succeeds" and "checkpoint advances".

### Replay on `start()` — Per-Handler Queue Transition

The replay-to-live transition uses per-handler queues to eliminate the concurrency flaw where a
handler could process replay and live events simultaneously.

```typescript
async start(): Promise<void> {
  // 1. Create partitions for current and next month
  const now = new Date();
  await this.log.ensurePartition(now);
  await this.log.ensurePartition(
    new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1)),
  );

  // 2. Load known partitions from pg_catalog
  await this.log.loadKnownPartitions();

  // 3. Create handler queues and register for live dispatch BEFORE replay
  const durableHandlers = this.handlers.filter(h => h.id !== undefined);
  for (const registration of durableHandlers) {
    registration.queue = new HandlerQueue();
    registration.queue.startDraining(
      registration.handler,
      registration.id,
      this.advanceCursor.bind(this),
      sql,
    );
  }

  // 4. Mark as started — emit() now enqueues to handler queues
  this.started = true;

  // 5. Replay each durable handler from its checkpoint (concurrent, each handler
  //    feeds its own queue). Uses REPEATABLE READ to freeze the MVCC snapshot.
  await Promise.all(
    durableHandlers.map(async (registration) => {
      const cursor = await this.getCursor(registration.id);

      await sql.begin("repeatable read", async (tx) => {
        const reader = cursor
          ? this.log.readAfter(cursor, { types: [registration.type], tx })
          : this.log.read({ types: [registration.type], tx });

        for await (const event of reader) {
          registration.queue.enqueueReplay(event);
        }
      });

      // Signal replay complete — queue will drain live events after replay
      registration.queue.replayComplete();
    }),
  );
}
```

**Why this is safe:**

1. **Queue created before `started = true`.** The handler's queue exists and its drain loop is
   running before `emit()` can enqueue live events. No events are missed.
2. **Replay-first drain order.** The drain loop always dequeues from `replayQueue` before
   `liveQueue`. Replay events have strictly lower ULIDs than live events (MVCC snapshot guarantee),
   so ULID order is preserved without sorting.
3. **ULID dedup catches the overlap window.** The MVCC snapshot includes events up to the moment
   the `REPEATABLE READ` transaction begins. Any event emitted after that moment goes to
   `liveQueue` via `enqueueLive()`. If an event falls in the narrow window where it appears in both
   the replay query result AND live dispatch, the `event.id <= lastProcessedId` guard skips the
   duplicate.
4. **No concurrent processing.** The drain loop awaits each handler call to completion before
   dequeuing the next event. No interleaving within a single handler.
5. **Handler emission during replay cannot deadlock.** If handler A emits a new event during its
   processing, `emit()` writes to PostgreSQL and synchronously enqueues to matching handler queues.
   It does NOT await handler completion. The new event enters handler A's `liveQueue` and processes
   after the current event completes.
6. **REPEATABLE READ for replay.** The replay query runs inside a `REPEATABLE READ` transaction,
   freezing the MVCC snapshot for the entire cursor iteration. This prevents postgres.js internal
   cursor batching from seeing different snapshots across batches.

### Late Handler Registration

Handlers registered after `start()` has completed route through the same queue mechanism. The
queue is created and registered for live dispatch BEFORE the background replay task starts,
ensuring no events are missed during the gap.

```typescript
on<T extends Event["type"]>(
  type: T,
  handler: Handler<Extract<Event, { type: T }>>,
  options?: HandlerOptions,
): void {
  const registration = { type, handler, id: options?.id };
  this.handlers.push(registration);

  // If the bus is already started and this is a durable handler, replay now
  if (this.started && registration.id !== undefined) {
    // 1. Create queue and start drain loop — live events enqueue immediately
    registration.queue = new HandlerQueue();
    registration.queue.startDraining(
      registration.handler,
      registration.id,
      this.advanceCursor.bind(this),
      sql,
    );

    // 2. Background replay feeds the queue (live events accumulate in liveQueue)
    void this.replayHandler(registration);
  }
}

private async replayHandler(registration: HandlerRegistration): Promise<void> {
  const cursor = await this.getCursor(registration.id);

  await sql.begin("repeatable read", async (tx) => {
    const reader = cursor
      ? this.log.readAfter(cursor, { types: [registration.type], tx })
      : this.log.read({ types: [registration.type], tx });

    for await (const event of reader) {
      registration.queue.enqueueReplay(event);
    }
  });

  registration.queue.replayComplete();
}
```

Late registration of a durable handler means full replay from the beginning (no cursor = no
checkpoint = start from zero). This is expected and correct — a new handler needs to build its
projection from scratch. For large event logs, this is expensive. Registering all handlers before
`start()` is preferred.

Ephemeral handlers registered after `start()` receive only live events — no replay, no checkpoint,
no queue. This is always the correct behavior for ephemeral handlers.

### Ephemeral Events

The bus supports ephemeral dispatch — events that skip the log and go directly to in-memory
subscribers. These use the `EphemeralEvent` type (Phase 2), which is type-incompatible with `Event`.

```typescript
emitEphemeral(event: EphemeralEvent): void;  // sync, fire-and-forget
onEphemeral<T extends EphemeralEvent["type"]>(
  type: T,
  handler: (event: Extract<EphemeralEvent, { type: T }>) => void,
): void;
```

No persistence, no checkpointing, no replay, no retry. For streaming chunks and internal signals
only.

### `stop()`

```typescript
private readonly SHUTDOWN_TIMEOUT_MS = 5000;

async stop(): Promise<void> {
  this.started = false;

  // Signal all handler queues to stop
  const durableHandlers = this.handlers.filter(h => h.queue !== undefined);
  for (const registration of durableHandlers) {
    registration.queue.stop();
  }

  // Wait for current in-flight event per handler to finish, with timeout
  const drainPromises = durableHandlers.map(h => h.queue.drained());
  await Promise.race([
    Promise.allSettled(drainPromises),
    new Promise(r => setTimeout(r, this.SHUTDOWN_TIMEOUT_MS)),
  ]);
}
```

**Semantics:** `stop()` finishes the event currently being processed by each handler, then
discards remaining queued events. It does NOT drain the full queue — those events will replay
from the checkpoint on next `start()`. The shutdown timeout prevents hanging on stuck handlers.

After `stop()`, `emit()` still writes to the event log (durability is unconditional) but does not
enqueue to handler queues. Events written while stopped will be replayed on the next `start()`.

## Definition of Done

- [ ] `eventLog.append()` writes an event to PostgreSQL and returns it with ID + timestamp
- [ ] `eventLog.append()` auto-creates the target partition if it does not exist
- [ ] `eventLog.read()` yields events in ULID order with upcasters applied
- [ ] `bus.emit()` writes to log AND enqueues to matching handler queues
- [ ] `bus.emit()` returns after durable write, not after handler processing
- [ ] `bus.emit({ ... }, { tx })` uses the provided transaction for the INSERT
- [ ] Durable handler (registered with `id`) gets a checkpoint in `handler_cursors`
- [ ] Ephemeral handler (registered without `id`) receives live events only, no checkpoint, no
  replay
- [ ] Handler queue processes replay events before live events
- [ ] Handler queue deduplicates events by EventId (ULID comparison)
- [ ] Checkpoint advancement is monotonic (`WHERE cursor < new_cursor`)
- [ ] Handler side effects + checkpoint advance are atomic (single transaction)
- [ ] Handler failure is caught — other handlers still run
- [ ] Handler failing 3 times on same event: dead-lettered, cursor advances, meta-event emitted
- [ ] Dead-letter emit + cursor advance are atomic (single transaction)
- [ ] `bus.start()` replays events from each durable handler's checkpoint
- [ ] Events emitted during replay are dispatched live (no duplicates, no gaps)
- [ ] Handler emission during replay does not deadlock
- [ ] Handler registered after `start()` triggers full replay for that handler
- [ ] `bus.stop()` finishes current event per handler, does not drain full queue
- [ ] `bus.flush()` drains all handler queues completely (testing only)
- [ ] Replay queries use REPEATABLE READ isolation
- [ ] Monthly partition auto-created on first write to a new month
- [ ] Partitions for current + next month created proactively on `start()`
- [ ] Ephemeral events dispatch without hitting the database
- [ ] `just check` passes

## Test Cases

### `tests/events/log.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Append returns complete event | Emit partial event | Returns event with ULID `id` and `timestamp` filled |
| Read returns events in order | Append 3 events | Read yields them in ULID order |
| ReadAfter skips past events | Append 5 events, readAfter(event3.id) | Yields events 4 and 5 only |
| Upcasters applied on read | v1 event in DB, upcaster 1->2 registered | Read yields v2 data |
| Type filter | Append mixed types, read with `types: ["message.received"]` | Only matching events |
| Partition auto-created | Append event with timestamp in a new month | Partition exists in pg_catalog, event readable |
| Append with tx | Pass a transaction to append | Event visible only after tx commits |
| Known partitions populated | Call loadKnownPartitions after creating partitions | Set contains all partition names |

### `tests/events/bus.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Emit enqueues to durable handler | Register handler with id, emit, flush | Handler called with event, checkpoint advanced |
| Emit dispatches to ephemeral handler | Register handler without id, emit | Handler called, no checkpoint row created |
| Handler isolation | Two handlers, first throws | Second still runs and succeeds |
| Checkpoint advances atomically | Durable handler succeeds | `handler_cursors` row updated in same tx as handler |
| Checkpoint never regresses | Emit 100 events, verify cursor after flush | Cursor advances monotonically |
| Dead-letter after retries | Handler always throws | After 3 failures: cursor advances, `system.handler.dead_lettered` emitted |
| Dead-letter atomicity | Handler always throws, inspect DB | Dead-letter event + cursor advance in same tx |
| Retry succeeds on second attempt | Handler throws once, then succeeds | Checkpoint advances, no dead-letter event |
| Replay on start | Events in DB, fresh durable handler | Handler receives all past events in order |
| Replay respects checkpoint | Events in DB, handler cursor at event3 | Handler receives events after event3 only |
| Concurrent emit during replay | Emit new events while start() replays | All events processed in ULID order, no gaps |
| Duplicate delivery skipped | Queue receives event with ULID <= last processed | Event skipped, handler not called |
| Handler emission during replay | Handler emits event while processing replay event | No deadlock, emitted event processed after current |
| Late handler registration | Register durable handler after start() | Handler replays from beginning, receives subsequent live events |
| Late ephemeral handler | Register ephemeral handler after start() | Handler receives only events emitted after registration |
| Emit with tx | Emit inside a transaction | Event persisted atomically with other tx writes |
| Stop mid-replay | Call stop() during replay | Current event finishes, restart replays correctly |
| Flush drains all queues | Emit events, call flush() | All handlers finish before flush() resolves |
| Ephemeral skip persistence | emitEphemeral() | No DB write, handler receives event |
| Partition proactive creation | Call start() | Partitions exist for current and next month |

## Risks

**Medium-high risk.** The "write then enqueue" invariant is critical — violating it means lost
events or phantom dispatches. The per-handler queue mechanism adds complexity but eliminates the
concurrency flaw in the previous design.

**Failure modes addressed:**

| ID | Mode | Severity | Mitigation |
| ---- | ------ | ---------- | ------------ |
| FM-1 | Crash between handler success + checkpoint | HIGH | Atomic tx (handler + checkpoint) |
| FM-8 | Non-idempotent handlers on replay | HIGH | Atomic tx (handler + checkpoint) |
| FM-9 | Queue re-entrancy | HIGH | `draining` flag + single drain loop |
| FM-10 | Dead-letter emit fails, cursor advanced | MEDIUM | Atomic tx (dead-letter + cursor) |
| FM-11 | MVCC timing window duplicate delivery | MEDIUM | ULID dedup in queue drain |
| FM-12 | postgres.js cursor batching breaks snapshot | LOW | REPEATABLE READ for replay |
| FM-5 | `stop()` with pending queue | MEDIUM | Discard + shutdown timeout |

**Mitigations:**

- Per-handler queues serialize all event processing — no concurrent handler execution.
- Replay-first drain order preserves ULID ordering without sorting.
- ULID dedup catches any MVCC timing window duplicates.
- Atomic handler+checkpoint transactions eliminate crash windows.
- REPEATABLE READ freezes the replay snapshot for the entire cursor iteration.
- The retry loop is bounded (3 attempts) and uses dead-lettering to prevent infinite loops.
- Partition auto-creation uses `IF NOT EXISTS` to be idempotent under concurrency.
- Integration tests verify the replay-to-live transition with concurrent emits.
- The `tx` parameter for atomic event+projection writes prevents the most common source of
  inconsistency in event-sourced systems.
