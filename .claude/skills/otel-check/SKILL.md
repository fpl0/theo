---
name: otel-check
description: Audit Theo's OpenTelemetry setup and verify signals are flowing correctly.
user-invocable: true
allowed-tools: Read, Bash, Grep, Glob
---

Audit Theo's OpenTelemetry setup and verify signals are flowing correctly.

## Steps

1. **Check infrastructure is running:**
   - `docker compose ps` — verify OpenObserve and PostgreSQL are healthy
   - `curl -s http://localhost:5080/healthz` — verify OpenObserve is reachable

2. **Verify OTEL configuration:**
   - Read `src/theo/config.py` and `.env.local` to confirm `otel_enabled=true` and `otel_exporter=otlp`
   - Verify `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS` are set correctly
   - Read `src/theo/telemetry.py` to confirm all three signals are initialized

3. **Check signal coverage across modules:**
   - Grep for `logging.getLogger` — every module under `src/theo/` should have a logger
   - Grep for `trace.get_tracer` — modules with I/O should have a tracer
   - Grep for `tracer.start_as_current_span` — verify spans exist on key operations
   - Check that asyncpg auto-instrumentation is enabled with `sanitize_query=True`

4. **Identify gaps:**
   - Modules missing loggers or tracers
   - Operations that should have spans but don't (DB writes, embedding calls, HTTP requests)
   - Missing span attributes that would aid debugging (e.g., `embed.count`, query identifiers)
   - Missing metrics (counters, histograms) for key operations

5. **Verify resource attributes:**
   - `service.name`, `service.version`, `host.name` should be set in the Resource

6. Present findings as a checklist: what's covered, what's missing, and specific code changes to close gaps.
