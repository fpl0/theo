# Event bus table placement

**Date:** 2026-03-26
**Ticket:** FPL-7

## Context

Theo's event bus needs a persistent event queue table. The initial proposal
used a dedicated PostgreSQL schema (`bus`) to separate infrastructure tables
from domain tables. After discussion, this was deemed unnecessary for a single
table.

## Decision

Keep the event queue table in the **`public`** schema alongside domain tables.
The table is named `event_queue` to clearly convey its purpose and avoid
collision with any future generic "event" concept.

## Alternatives considered

1. **Separate `bus` schema** — clean isolation, but overkill for one table.
   Can revisit if the bus grows to multiple tables.
2. **Prefix convention** (`bus_event`) — adds no real value over a descriptive
   name like `event_queue`.

## Consequences

- All tables remain in `public`. No schema management overhead.
- If infrastructure tables proliferate in the future, a dedicated schema can
  be introduced via a new migration.
