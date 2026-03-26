---
paths: ["src/theo/**/*.py"]
---

# Telemetry conventions

- `init_telemetry()` bootstraps all signals. `shutdown_telemetry()` flushes on exit.
- Exporter is chosen via `THEO_OTEL_EXPORTER`: `"console"` (dev) or `"otlp"` (production).
- OTLP exports to OpenObserve at `http://localhost:5080/api/default` with Basic auth.
- asyncpg queries use `sanitize_query=True` to avoid leaking parameters in spans.

## Tracing

- Every module gets a tracer: `tracer = trace.get_tracer(__name__)`. No exceptions.
- Every public function that does I/O (database, network, embedding) must be wrapped in `tracer.start_as_current_span()`. asyncpg auto-instrumentation will nest under application spans automatically.
- Span names should describe the operation, not the implementation: `"retrieve_nodes"` not `"run_select_query"`.
- Add semantic attributes to spans: `node.kind`, `session.id`, `embed.count`, etc.

## Metrics

- Use histograms for latencies, counters for throughput, gauges for pool/queue sizes.
- Name metrics with the `theo.` prefix: `theo.retrieval.duration`, `theo.nodes.count`.
- Pick the right instrument: **Counter** for monotonic totals, **Histogram** for latency/p99, **UpDownCounter** for values that go up and down, **Gauge** (via async callback) for point-in-time snapshots of external state.
- Avoid metric explosion: do not create per-node-kind or per-session metrics. Use span attributes for that cardinality — metrics are for aggregate signals, traces are for per-request detail.

## Logging

- Structured key-value context over free-form messages: `log.info("stored node", extra={"node_id": id, "kind": kind})`.
- Log at boundaries: entry/exit of operations, errors, and state transitions.
