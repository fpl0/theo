# SyntheticProbeFailing

## What it means

The synthetic prober is issuing canary turns via the scheduler and they
are failing. This catches the "alive but stuck" failure mode that
`launchd`'s process-level checks miss.

## Triage

1. Look at the most recent `synthetic.probe.completed` event — the `reason`
   field indicates `timeout`, `not_ok`, or `exception`.
2. If the reason is `timeout`, check for event-loop lag or DB stalls.
3. If `not_ok`, the SDK itself is returning a failed TurnResult — inspect
   the advisor / subagent path.

## Resolution

- **Timeout class:** investigate the hot path via Pyroscope; the event
  loop is starved. Reduce concurrency or move work off the main loop.
- **not_ok class:** confirm Anthropic API credentials, model access, and
  network connectivity.
- **exception class:** read the stack trace from the logs.

## Related

- Dashboard: [Overview](http://localhost:3000/d/theo-overview)
- Source: `src/telemetry/synthetic.ts` (to be added), `src/scheduler/`
