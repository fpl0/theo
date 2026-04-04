---
paths: ["src/events/**"]
---

# Event system conventions

## Events are immutable

Events in the log are never modified. Once written, they exist forever. Schema evolution happens through upcasters, not data migrations.

## Event structure

Every event implements `TheoEvent<Type, Data>` with:
- `id`: ULID (assigned at emit time)
- `type`: string literal (discriminant for the union)
- `version`: number (schema version for this event type)
- `timestamp`: Date
- `actor`: "user" | "theo" | "scheduler" | "system"
- `data`: typed payload
- `metadata`: traceId, sessionId, causeId, gate

## Adding a new event type

1. Define the event type as a new member of the discriminated union in the appropriate group (ChatEvent, MemoryEvent, SchedulerEvent, SystemEvent)
2. Add it to the top-level `Event` union
3. Register handlers in the bus
4. Start at version 1

## Upcasters

When an event type's schema changes:
1. Increment the version number in new emissions
2. Register an upcaster: `upcasters.register("event.type", oldVersion, transformFn)`
3. The upcaster transforms old shape to new shape at read time
4. Old events in the log stay untouched

## Bus invariants

- Durable events: INSERT into events table THEN dispatch to handlers. Write before dispatch, always.
- Handlers are idempotent by convention. Processing the same event twice must be safe.
- Handler failures are isolated — one failing handler never blocks others.
- Dead-letter after configurable retry count. The failure itself is recorded.
- Ephemeral events use `EphemeralEvent` type — the type system prevents accidentally skipping persistence for durable events.
