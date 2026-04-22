# HandlerLag

## What it means

At least one durable handler's checkpoint is more than five minutes behind
the log tail. Cascading events (memory consolidation, pattern synthesis)
may stop firing until the lag clears.

## Triage

1. Open the Bus dashboard: which `handler` label carries the lag?
2. Check `theo_bus_handler_errors_total` — is the handler crashing in a
   retry loop, or simply slow?

## Resolution

- **If crashing:** inspect the handler's code and the triggering events;
  the dead-letter table captures the last failure payload.
- **If slow:** profile via Pyroscope — the flame graph identifies the hot
  path.

## Related

- Dashboard: [Bus](http://localhost:3000/d/theo-bus)
- Source: `src/events/bus.ts`, `src/events/queue.ts`
