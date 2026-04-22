# SLOFastBurn

## What it means

A 1h + 5m burn-rate window shows the turn-availability SLO is consuming
budget at >14.4× the allowed rate — unchecked, the 30d budget would be
exhausted in under two days.

## Triage

1. Open the SLOs dashboard and confirm both windows (1h + 5m) agree.
2. Drill into the failing gate: which `gate` label carries the errors?
3. Check `theo:turns:error_rate_5m` — is the rate climbing or stable?

## Resolution

- **Recent self-update:** check the latest commits — the fast burn often
  follows a bad merge. Roll back manually via `just rollback` if the
  auto-rollback path didn't fire.
- **Remote dependency failure:** the Anthropic API or a tool remote may be
  degraded; consider pausing the executive/reflex classes via
  `/degradation 3 api_outage`.
- **Resource pressure:** check the Process panel for memory/CPU saturation.

## Related

- Dashboard: [SLOs](http://localhost:3000/d/theo-slos)
- Source: `src/telemetry/slos.ts`
