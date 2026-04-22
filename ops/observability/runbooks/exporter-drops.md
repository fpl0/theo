# ExporterDropping

## What it means

The telemetry SDK's span or metric exporter queue has overflowed; payloads
are being dropped rather than blocking the main loop. Observability is
degrading while the agent keeps working.

## Triage

1. Check `theo_telemetry_exporter_queue_saturation_gauge` — is the queue
   pinned at 1.0 or just intermittently full?
2. Verify the collector is up (`docker compose ps` under
   `ops/observability`).
3. Inspect collector logs for export-backoff messages.

## Resolution

- **If the collector is down:** restart it; the drop counter should level
  off once the queue drains.
- **If the collector is up but slow:** increase its batch size or the
  scheduled delay, or scale the downstream (Prometheus, Tempo).
- **If Theo is generating more spans than the queue can absorb:** raise
  `maxQueueSize` in `exporters.ts` or sample more aggressively.

## Related

- Dashboard: [Meta](http://localhost:3000/d/theo-meta)
- Source: `src/telemetry/tracer.ts`, `src/telemetry/metrics.ts`
