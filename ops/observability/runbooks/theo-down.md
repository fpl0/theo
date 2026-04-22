# TheoDown

## What it means

Theo has produced neither a user-facing turn nor a reflex dispatch for 15
minutes. `launchd` may still see a live process, but the agent is not doing
anything — either it's deadlocked, blocked on a remote dependency, or its
in-process queues are jammed.

## Triage

1. Check the Grafana Overview dashboard: is the `Turns / 5m` stat truly zero
   or is Prometheus failing to scrape?
2. Query `up{job="theo"}` in Prometheus — a `0` here means the scrape target
   itself is unreachable; this is a meta-layer problem, not Theo.
3. Inspect the most recent entries in `~/Theo/logs/theo-*.log`. A flood of
   handler errors usually accompanies the stall.
4. `launchctl list | grep com.theo.agent` — is the PID changing (flapping)
   or stuck?

## Resolution

- **If the process is flapping:** `launchctl unload` the plist, investigate
  the stderr log, restore with `launchctl load` once the cause is fixed.
- **If the process is stuck:** send SIGQUIT to get a stack dump, then
  `launchctl kickstart -k gui/$(id -u)/com.theo.agent` to force a restart.
- **If Prometheus can't scrape:** verify the collector is up
  (`docker compose ps` under `ops/observability`).

## Related

- Dashboard: [Overview](http://localhost:3000/d/theo-overview)
- Source: `src/engine.ts`, `src/index.ts`
