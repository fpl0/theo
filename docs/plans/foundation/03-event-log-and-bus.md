# Phase 3: Event Log & Bus

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
| `src/events/handlers.ts` | Handler type, dispatch with retry loop, dead-letter logic |
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
  stop(): Promise<void>;   // drain in-flight handlers, flush
}

type Handler<E extends Event> = (event: E) => Promise<void>;
```

**Durable vs ephemeral handlers.** Handlers registered with an `id` option are durable: they get a
checkpoint in `handler_cursors`, replay missed events on `start()`, and survive restarts. Handlers
registered without `id` are ephemeral: they receive only live events after registration, have no
checkpoint, and no replay. Ephemeral handlers are for non-critical side effects like logging and
metrics. Any handler that affects durable state MUST have a stable id.

### Transaction Boundary

The event INSERT is committed BEFORE handler dispatch begins. This is the critical invariant.

```typescript
async emit(
  event: Omit<Event, "id" | "timestamp">,
  options?: { tx?: Transaction },
): Promise<Event> {
  // 1. Write to PostgreSQL — committed on return (or when caller's tx commits)
  const persisted = await this.log.append(event, options?.tx);

  // 2. Dispatch to in-memory handlers — only after successful write
  await this.dispatch(persisted);

  return persisted;
}
```

If the INSERT fails (constraint violation, partition missing, connection error), no dispatch happens
— no phantom events. If dispatch fails (a handler throws), the event exists in PostgreSQL — correct,
because write-before-dispatch guarantees durability. The failed handler will retry or dead-letter,
but the event is never lost.

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

All handlers matching the event type are dispatched concurrently via `Promise.allSettled`. One
failing handler never blocks others.

```typescript
async dispatch(event: Event): Promise<void> {
  const matching = this.handlers.filter(h => h.type === event.type);
  const results = await Promise.allSettled(
    matching.map(h => this.executeWithRetry(h, event)),
  );
  // Failures are handled inside executeWithRetry (retry or dead-letter).
  // No re-throw — dispatch never fails from the caller's perspective.
}
```

### Retry Strategy

Failed handlers are retried immediately on the SAME event. No delays between retries — these are
in-memory operations, not network calls. A transient failure (e.g., a race condition, a temporary
resource issue) is resolved by immediate retry. A permanent failure (e.g., a bug in the handler)
fails fast after 3 attempts.

```typescript
// Map of "handlerId:eventId" → failure count
private failureCounts = new Map<string, number>();

private readonly MAX_RETRIES = 3;

async executeWithRetry(registration: HandlerRegistration, event: Event): Promise<void> {
  const key = registration.id
    ? `${registration.id}:${event.id}`
    : undefined; // ephemeral handlers: no tracking, no retry

  if (!key) {
    // Ephemeral handler: fire-and-forget, catch and log errors
    try {
      await registration.handler(event);
    } catch (error) {
      // Log but do not retry — ephemeral handlers are best-effort
    }
    return;
  }

  for (let attempt = 1; attempt <= this.MAX_RETRIES; attempt++) {
    try {
      await registration.handler(event);

      // Success — advance checkpoint and clean up
      await this.advanceCursor(registration.id, event.id);
      this.failureCounts.delete(key);
      return;
    } catch (error) {
      this.failureCounts.set(key, attempt);

      if (attempt === this.MAX_RETRIES) {
        // Dead-letter: advance cursor past the problematic event
        await this.advanceCursor(registration.id, event.id);
        this.failureCounts.delete(key);

        // Emit a meta-event recording the failure
        await this.log.append({
          type: "system.handler.dead_lettered",
          version: 1,
          actor: "system",
          data: {
            handlerId: registration.id,
            eventId: event.id,
            attempts: this.MAX_RETRIES,
            lastError: error instanceof Error ? error.message : String(error),
          },
          metadata: {},
        });
        // Note: the dead-letter event is dispatched to handlers on its own
        // (recursive emit). This is safe because dead-letter events themselves
        // are unlikely to fail, and if they do, the same retry logic applies.
        return;
      }
      // Otherwise: loop immediately to next attempt
    }
  }
}
```

**Why no backoff:** Theo is a single-user, single-machine system. Handlers are in-memory functions
operating on local state. There is no remote service to back off from. If a handler fails 3 times in
a row, it has a bug — backing off won't help.

### Checkpointing

Each durable handler (registered with a stable `id`) gets a cursor in `handler_cursors`. The cursor
holds the ULID of the last event successfully processed by that handler.

```typescript
async advanceCursor(handlerId: string, eventId: EventId): Promise<void> {
  await sql`
    INSERT INTO handler_cursors (handler_id, cursor, updated_at)
    VALUES (${handlerId}, ${eventId}, now())
    ON CONFLICT (handler_id)
    DO UPDATE SET cursor = ${eventId}, updated_at = now()
  `;
}

async getCursor(handlerId: string): Promise<EventId | null> {
  const rows = await sql`
    SELECT cursor FROM handler_cursors WHERE handler_id = ${handlerId}
  `;
  return rows[0]?.cursor ?? null;
}
```

### Replay on `start()` — Solving the Race

The replay-to-live transition must handle events emitted during replay without duplicates or gaps.
The design uses PostgreSQL's MVCC guarantees to avoid a race condition.

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

  // 3. Replay each durable handler from its checkpoint
  const durableHandlers = this.handlers.filter(h => h.id !== undefined);

  // Replay handlers concurrently (each handler replays sequentially within itself)
  await Promise.all(
    durableHandlers.map(async (registration) => {
      const cursor = await this.getCursor(registration.id);

      // Read events after the cursor. This query takes a snapshot of the events
      // table at this moment (PostgreSQL MVCC). Any events emitted during replay
      // by other handlers or external callers are NOT visible to this query —
      // they will be dispatched live to this handler via the normal emit() path.
      const reader = cursor
        ? this.log.readAfter(cursor, { types: [registration.type] })
        : this.log.read({ types: [registration.type] });

      for await (const event of reader) {
        await this.executeWithRetry(registration, event);
      }
    }),
  );

  // 4. Mark as started — from this point, all handlers receive live events
  this.started = true;
}
```

**Why there is no race:**

- During replay, `emit()` is fully operational. New events are written to PostgreSQL and dispatched
  to ALL registered handlers (including handlers currently replaying).
- The replay query uses a snapshot (MVCC) — it only sees events that existed when the query started.
  Events written during replay are invisible to the replay cursor.
- Those events written during replay ARE dispatched to the replaying handler via the normal `emit()`
  → `dispatch()` path, because `dispatch()` sends to all matching handlers regardless of replay
  state.
- This means the replaying handler processes old events sequentially via the replay loop AND new
  events concurrently via live dispatch. The checkpoint cursor tracks progress. Since ULIDs are
  monotonically increasing and the checkpoint only advances forward, a handler never processes the
  same event twice.

**What about events emitted during replay by handler dispatch?** If handler A's replay of event X
causes a new event Y to be emitted, event Y is written to PostgreSQL and dispatched to all handlers
(including handler B which might also be replaying). Handler B will see event Y via live dispatch if
B has already passed that point in its replay, or via its own replay query if Y's ULID falls within
the replay range. Both cases are correct.

### Late Handler Registration

Handlers registered after `start()` has completed trigger a full replay for that handler. A handler
with no checkpoint in `handler_cursors` has never processed any events, so it starts from the
beginning of the event log.

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
    // Fire-and-forget replay — runs in background, handler receives live
    // events immediately via dispatch() while replay catches up on old ones
    void this.replayHandler(registration);
  }
}

private async replayHandler(registration: HandlerRegistration): Promise<void> {
  const cursor = await this.getCursor(registration.id);
  const reader = cursor
    ? this.log.readAfter(cursor, { types: [registration.type] })
    : this.log.read({ types: [registration.type] });

  for await (const event of reader) {
    await this.executeWithRetry(registration, event);
  }
}
```

Late registration of a durable handler means full replay from the beginning (no cursor = no
checkpoint = start from zero). This is expected and correct — a new handler needs to build its
projection from scratch. For large event logs, this is expensive. Registering all handlers before
`start()` is preferred.

Ephemeral handlers registered after `start()` receive only live events — no replay, no checkpoint.
This is always the correct behavior for ephemeral handlers.

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
async stop(): Promise<void> {
  this.started = false;
  // Wait for any in-flight dispatch operations to complete
  // (tracked via a Set<Promise<void>> of active dispatches)
  await Promise.allSettled([...this.inflight]);
}
```

After `stop()`, `emit()` still writes to the event log (durability is unconditional) but does not
dispatch to handlers. Events written while stopped will be replayed on the next `start()`.

## Definition of Done

- [ ] `eventLog.append()` writes an event to PostgreSQL and returns it with ID + timestamp
- [ ] `eventLog.append()` auto-creates the target partition if it does not exist
- [ ] `eventLog.read()` yields events in ULID order with upcasters applied
- [ ] `bus.emit()` writes to log AND dispatches to registered handlers
- [ ] `bus.emit({ ... }, { tx })` uses the provided transaction for the INSERT
- [ ] Durable handler (registered with `id`) gets a checkpoint in `handler_cursors`
- [ ] Ephemeral handler (registered without `id`) receives live events only, no checkpoint, no
  replay
- [ ] After successful handler execution, checkpoint advances
- [ ] Handler failure is caught — other handlers still run
- [ ] Handler failing 3 times on same event: dead-lettered, cursor advances, meta-event emitted
- [ ] `bus.start()` replays events from each durable handler's checkpoint
- [ ] Events emitted during replay are dispatched live (no duplicates, no gaps)
- [ ] Handler registered after `start()` triggers full replay for that handler
- [ ] `bus.stop()` drains in-flight handlers before resolving
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
| Emit dispatches to durable handler | Register handler with id, emit matching event | Handler called with event, checkpoint advanced |
| Emit dispatches to ephemeral handler | Register handler without id, emit matching event | Handler called, no checkpoint row created |
| Handler isolation | Two handlers, first throws | Second still runs and succeeds |
| Checkpoint advances | Durable handler succeeds | `handler_cursors` row updated to event ULID |
| Dead-letter after retries | Handler always throws | After 3 failures: cursor advances, `system.handler.dead_lettered` event emitted |
| Retry succeeds on second attempt | Handler throws once, then succeeds | Checkpoint advances, no dead-letter event |
| Replay on start | Events in DB, fresh durable handler | Handler receives all past events in order |
| Replay respects checkpoint | Events in DB, handler cursor at event3 | Handler receives events after event3 only |
| Emit during replay | Emit a new event while start() is replaying | Replaying handler sees old events from replay AND new event via live dispatch |
| Late handler registration | Register durable handler after start() | Handler replays from beginning, receives subsequent live events |
| Late ephemeral handler | Register ephemeral handler after start() | Handler receives only events emitted after registration |
| Emit with tx | Emit inside a transaction | Event persisted atomically with other tx writes |
| Stop drains handlers | Emit event, call stop() | Handlers finish before stop() resolves |
| Ephemeral skip persistence | emitEphemeral() | No DB write, handler receives event |
| Partition proactive creation | Call start() | Partitions exist for current and next month |

## Risks

**Medium-high risk.** The "write then dispatch" invariant is critical — violating it means lost
events or phantom dispatches. The replay-to-live transition relies on PostgreSQL MVCC guarantees
that must be verified with integration tests, not just unit tests.

**Mitigations:**

- The transaction boundary is explicit and simple: INSERT commits, then dispatch runs. No
  interleaving.
- MVCC snapshot isolation means replay queries are immune to concurrent writes — this is a
  PostgreSQL guarantee, not application-level logic.
- The retry loop is bounded (3 attempts) and uses dead-lettering to prevent infinite loops.
- Partition auto-creation uses `IF NOT EXISTS` to be idempotent under concurrency.
- Integration tests verify the replay-to-live transition with concurrent emits.
- The `tx` parameter for atomic event+projection writes prevents the most common source of
  inconsistency in event-sourced systems.
