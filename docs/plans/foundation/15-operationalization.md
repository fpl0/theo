# Phase 15: Operationalization

## Cross-cutting dependencies

Observability must cover every layer of Theo — chat, bus, memory, scheduler, the autonomous
agency layer (Phases 12a and 13b), the advisor (Phase 14), and the process itself. The unified
metric set below is the canonical list; all instruments are defined once in
`src/telemetry/metrics.ts`.

| Metric | Type | Phase | Tracks |
| ------ | ---- | ----- | ------ |
| `theo.turns.total` | Counter | 15 | Turns (by gate) |
| `theo.turns.duration_ms` | Histogram | 15 | Turn latency (by gate) |
| `theo.tokens.input` | Counter | 15 | Input tokens (by model) |
| `theo.tokens.output` | Counter | 15 | Output tokens (by model) |
| `theo.cost.usd` | Counter | 15 | Cumulative model cost (by model, role) |
| `theo.retrieval.duration_ms` | Histogram | 15 | RRF retrieval latency |
| `theo.cache.hit_rate_gauge` | Gauge | 15 | Embedding cache hit rate |
| `theo.memory.nodes_gauge` | Gauge | 15 | Node table size |
| `theo.memory.embedding_bytes_gauge` | Gauge | 15 | Vector storage footprint |
| `theo.bus.events_appended_total` | Counter | 15 | Events appended (by type) |
| `theo.bus.handler_duration_ms` | Histogram | 15 | Per-handler latency (by handler) |
| `theo.bus.handler_errors_total` | Counter | 15 | Handler failures (by handler, reason) |
| `theo.bus.handler_lag_seconds` | Gauge | 15 | Handler checkpoint lag (by handler) |
| `theo.db.query_duration_ms` | Histogram | 15 | DB query latency (by label) |
| `theo.db.pool_in_use_gauge` | Gauge | 15 | Connection pool in-use |
| `theo.scheduler.tick_duration_ms` | Histogram | 15 | Scheduler tick cost |
| `theo.scheduler.jobs_due_gauge` | Gauge | 15 | Jobs waiting to run |
| `theo.process.memory_rss_bytes` | Gauge | 15 | Resident memory |
| `theo.process.event_loop_lag_ms` | Gauge | 15 | Event loop responsiveness |
| `theo.goals.active_gauge` | Gauge | 12a | Currently active goals |
| `theo.goals.quarantined_gauge` | Gauge | 12a | Quarantined goals awaiting owner |
| `theo.goals.task_turns_total` | Counter | 12a | Executive task turns (by status) |
| `theo.goals.lease_contention_total` | Counter | 12a | Failed lease acquisitions |
| `theo.reflex.received_total` | Counter | 13b | Webhooks received (by source) |
| `theo.reflex.rejected_total` | Counter | 13b | Rejected (by reason) |
| `theo.reflex.rate_limited_total` | Counter | 13b | Rate limited (by source) |
| `theo.reflex.dispatched_total` | Counter | 13b | Reflex turns dispatched |
| `theo.ideation.runs_total` | Counter | 13b | Ideation runs (by outcome) |
| `theo.ideation.cost_usd_total` | Counter | 13b | Cumulative ideation cost |
| `theo.ideation.proposals_total` | Counter | 13b | Proposals generated |
| `theo.proposals.pending_gauge` | Gauge | 13b | Pending proposals awaiting approval |
| `theo.proposals.approved_total` | Counter | 13b | Approved (by kind) |
| `theo.proposals.expired_total` | Counter | 13b | Expired without approval |
| `theo.cloud_egress.cost_usd_total` | Counter | 13b | Cumulative autonomous cloud spend |
| `theo.cloud_egress.tokens_total` | Counter | 13b | Cumulative autonomous cloud tokens |
| `theo.degradation.level_gauge` | Gauge | 13b | Current degradation level (0-4) |
| `theo.autonomy.violations_total` | Counter | 13b | Denylist violations by domain |
| `theo.advisor.iterations_total` | Counter | 14 | Advisor sub-inferences (by subagent) |
| `theo.advisor.cost_usd_total` | Counter | 14 | Cumulative advisor cost (by model) |
| `theo.telemetry.exporter_dropped_total` | Counter | 15 | Spans/metrics dropped due to exporter backpressure (by signal) |
| `theo.telemetry.exporter_queue_saturation_gauge` | Gauge | 15 | Exporter queue fill ratio 0-1 (by signal) |
| `theo.telemetry.redactions_total` | Counter | 15 | Attribute redactions performed (by key) |
| `theo.telemetry.cardinality_rejections_total` | Counter | 15 | Label values rejected by closed-set guard (by metric, label) |
| `theo.synthetic.probe_duration_ms` | Histogram | 15 | Canary probe round-trip latency |
| `theo.synthetic.probe_failures_total` | Counter | 15 | Canary probe failures (by reason) |
| `theo.slo.error_budget_remaining_ratio` | Gauge | 15 | Error budget remaining 0-1 (by SLO) |
| `theo.slo.burn_rate` | Gauge | 15 | Current burn rate over short window (by SLO, window) |

Every metric is tagged with the global resource attributes `service.name=theo`,
`service.version=<git sha>`, `service.instance.id=<hostname>`, `deployment.environment`. Labels
on each metric are drawn from a closed-set enum (see **Cardinality discipline**).

Phase 15 also owns the **degradation healing timer** that emits `degradation.level_changed`
events when conditions improve for a configurable window (see `foundation.md §7.5`). The healing
timer runs as a periodic task inside the engine's scheduler tick loop.

The telemetry projector (see below) reads `usage.iterations[]` from every `turn.completed` event
and emits one sample per iteration type (`message` vs `advisor_message`) so cost dashboards can
break down executor vs advisor contribution.

## Motivation

Theo runs on macOS as an always-on process with complete OS access, access to its own source code
(GitHub), and a dedicated workspace. It must survive reboots, crashes, and self-updates. Without
proper operationalization, Theo is a development prototype that dies on restart and provides no
visibility into what it's doing.

This phase makes Theo production-ready for a single-machine deployment: always running via
`launchd`, self-updating with rollback safety, and **fully observable through a single telemetry
framework** that keeps observability out of business code. Metrics, traces, logs, dashboards, and
alerts all flow through one module and one Grafana stack. A small in-repo read-only web UI
provides typed domain views (event browser, goal inspector, proposal queue) for the things
Grafana cannot do well.

## Depends on

- **Phase 14** — Engine lifecycle (startup/shutdown sequence)
- **Phase 11** — CLI gate (for auth on the views server)
- **Phase 12** — Scheduler (background jobs need observability)

## Framework philosophy

The cardinal rule: **observability does not bleed into business code.** Code paths sprinkled
with `logger.info`, `metrics.increment`, and `tracer.startSpan` are a framework failure — they
couple domains to the telemetry stack, invite drift, and make instrumentation an afterthought.

Theo's architecture already provides the primitive that makes this avoidable: **the event log is
an observability stream.** Every meaningful state change is an event. Therefore metrics, logs,
and traces are *projections* of the event stream — just like memory is a projection.

### Principle 1 — Events are the substrate

A single **telemetry projector** handler subscribes to the bus and derives signals from domain
events. Adding a metric is a one-line change to the projector, not a sweep across the codebase.

### Principle 2 — Three narrow integration surfaces

Business code touches telemetry in exactly three ways:

1. **Emit an event.** Already the domain's job. The projector derives logs/metrics/traces.
2. **Wrap a boundary with `withSpan`.** Only at system boundaries (chat turn, SDK call,
   retrieval, DB query, bus dispatch). Code inside the span is untouched.
3. **Register an observable gauge.** Periodic DB pulls live in `src/telemetry/gauges.ts` —
   nowhere else.

No other module imports `@opentelemetry/*`, the logger, or the metrics registry. A biome
`noRestrictedImports` rule enforces this at lint time.

### Principle 3 — Telemetry is a module, not a cross-cutting concern

`src/telemetry/` is self-contained. Its public API is `TelemetryBundle` — a tight struct handed
to the engine at startup. Subsystems receive only what they need via DI (the chat engine gets a
`withSpan` helper; that's it).

### Principle 4 — Dashboards and alerts are code

Grafana dashboards, alert rules, and datasource/contact-point configuration live in-repo under
`ops/observability/`. Grafana's file-based provisioning loads them at startup. A dashboard
change is reviewed like any other change; `just check` validates JSON.

### Principle 5 — Grafana is the single surface

One UI: Grafana. Metrics, traces, and logs via Prom/Tempo/Loki; ad-hoc event queries via the
Postgres datasource; profiles via Pyroscope. Keeps operational surface area small.

The tradeoff is **upcaster fidelity** — Grafana reads raw stored events, not the upcast shape
the agent sees. For events that have been through a schema migration (upcaster), the raw row
in the events panel may differ from what business code operates on. When precise upcast shape
matters, read through the TypeScript types rather than the raw panel. A typed custom views
layer may be added in a future phase if this limitation bites in practice; it is out of scope
for Phase 15.

### Principle 6 — Signals are correlated end-to-end

Drilling from alert → metric → trace → log → event → profile is one click. This requires:

- **Metric exemplars.** Every histogram observation is annotated with the active `traceId`.
- **Trace/log correlation.** Every log carries `traceId`/`spanId` from the active context.
- **Trace/event correlation.** Every event carries `metadata.traceId`/`metadata.spanId` from
  the emitting context; bus dispatch *rehydrates* the context before invoking a handler, so
  handler spans nest under the emitter.
- **Shared resource attributes.** `service.name`, `service.version=<git sha>`,
  `service.instance.id`, `deployment.environment` are attached to every span, metric, log,
  and profile at the SDK level.
- **OTel semantic conventions.** Standard attribute names (`db.system`, `db.operation`,
  `messaging.system`, `messaging.destination`, `code.function`) are used wherever applicable.

### Principle 7 — Observability never degrades the agent

The telemetry pipeline is bounded, backpressure-safe, and PII-safe:

- Span and metric exporter queues have fixed sizes. When full, spans are **dropped and
  counted** (`theo.telemetry.exporter_dropped_total`) — never blocked.
- A collector outage does not block the main loop. The SDK is fire-and-forget beyond its
  queues.
- Attribute values pass through a **redaction span processor** before export. Only keys on an
  explicit allowlist survive; everything else is replaced with `[redacted]` and counted
  (`theo.telemetry.redactions_total`). Bodies never leave the process.
- Label values pass through a **closed-set guard**. Unknown values are rejected, counted, and
  replaced with `unknown` — cardinality cannot explode.

## Scope

### Files to create

| File | Purpose |
| ---- | ------- |
| `src/telemetry/index.ts` | `initTelemetry()` — builds `TelemetryBundle`, wires projector and gauges |
| `src/telemetry/logger.ts` | Structured JSON logger (module-internal) |
| `src/telemetry/tracer.ts` | OTel tracer bootstrap + `withSpan` helper |
| `src/telemetry/metrics.ts` | Meter + `MetricsRegistry` (all instruments defined once) |
| `src/telemetry/projector.ts` | Bus handler: domain events → metrics/logs/trace events |
| `src/telemetry/gauges.ts` | Periodic observable gauges (DB-backed pulls) |
| `src/telemetry/resource.ts` | Resource attribute builder (service.name, version, instance, env) |
| `src/telemetry/labels.ts` | Closed-set label enums + `assertLabel` guard |
| `src/telemetry/redact.ts` | Attribute allowlist + redaction span/log processor |
| `src/telemetry/context.ts` | Async context propagation: extract/inject `traceId` on events, rehydrate on dispatch |
| `src/telemetry/semconv.ts` | OTel semantic convention constants used across the module |
| `src/telemetry/sampling.ts` | Tail-based sampling policy (keep all error traces, sample success) |
| `src/telemetry/slos.ts` | SLI/SLO definitions (as code) + burn-rate window specs |
| `src/telemetry/profiling.ts` | Pyroscope client (continuous heap + CPU profiling) |
| `src/telemetry/synthetic.ts` | Canary prober: sends a periodic self-test turn, records probe metrics |
| `src/telemetry/spans/chat.ts` | Turn lifecycle span boundary (semconv-compliant) |
| `src/telemetry/spans/retrieval.ts` | Retrieval span boundary |
| `src/telemetry/spans/sdk.ts` | SDK query span boundary |
| `src/telemetry/spans/bus.ts` | Per-handler span wrapper + context rehydration from event metadata |
| `src/telemetry/spans/db.ts` | Postgres.js query-timing hook (emits `db.*` semconv attrs) |
| `src/telemetry/exporters.ts` | OTLP / no-op exporter selection + bounded queue config |
| `src/selfupdate/healthcheck.ts` | `just check` runner, healthy_commit tracking |
| `src/selfupdate/rollback.ts` | Git rollback to healthy_commit on startup failure |
| `ops/com.theo.agent.plist` | `launchd` plist for always-on macOS deployment |
| `ops/install.sh` | One-shot setup: workspace dirs, plist, optional observability stack |
| `ops/observability/docker-compose.yaml` | Local LGTM stack (Grafana + Loki + Tempo + Prometheus + OTel Collector) |
| `ops/observability/otel-collector/config.yaml` | Collector pipeline config |
| `ops/observability/prometheus/prometheus.yaml` | Prometheus scrape config (including self-scrape + collector scrape) |
| `ops/observability/prometheus/recording_rules.yaml` | Recording rules (precomputed p50/p95/p99, SLO error rates) |
| `ops/observability/pyroscope/pyroscope.yaml` | Continuous profiling backend config |
| `ops/observability/loki/loki.yaml` | Loki storage config |
| `ops/observability/tempo/tempo.yaml` | Tempo storage config |
| `ops/observability/promtail/promtail.yaml` | Tails `~/Theo/logs/` into Loki |
| `ops/observability/grafana/provisioning/datasources/datasources.yaml` | Prom, Loki, Tempo, Postgres |
| `ops/observability/grafana/provisioning/dashboards/dashboards.yaml` | Dashboard provider config |
| `ops/observability/grafana/dashboards/overview.json` | Health at a glance |
| `ops/observability/grafana/dashboards/cost.json` | Executor vs advisor vs cloud spend |
| `ops/observability/grafana/dashboards/turns.json` | Latency, tokens, gate breakdown |
| `ops/observability/grafana/dashboards/goals.json` | Active/quarantined, task-turn success |
| `ops/observability/grafana/dashboards/proposals.json` | Pending, approval rate, expiry |
| `ops/observability/grafana/dashboards/reflex.json` | Webhook throughput, rejections |
| `ops/observability/grafana/dashboards/autonomy.json` | Violations, degradation history |
| `ops/observability/grafana/dashboards/bus.json` | Handler latency, lag, errors |
| `ops/observability/grafana/dashboards/events.json` | Event browser (Postgres datasource; raw shape — not upcast) |
| `ops/observability/grafana/dashboards/slos.json` | SLO burn-rate dashboard (per SLO: SLI, error budget, burn) |
| `ops/observability/grafana/dashboards/profiling.json` | Pyroscope flame graph views |
| `ops/observability/grafana/dashboards/meta.json` | Meta-observability: collector, Prometheus, Loki, Tempo self-metrics |
| `ops/observability/grafana/provisioning/alerting/rules.yaml` | Alert rule definitions (SLO burn-rate + threshold guards) |
| `ops/observability/grafana/provisioning/alerting/contactpoints.yaml` | Telegram + email |
| `ops/observability/grafana/provisioning/alerting/policies.yaml` | Routing policy (with silence/maintenance windows) |
| `ops/observability/runbooks/theo-down.md` | Runbook: TheoDown alert |
| `ops/observability/runbooks/slo-burn-fast.md` | Runbook: fast burn-rate alert |
| `ops/observability/runbooks/slo-burn-slow.md` | Runbook: slow burn-rate alert |
| `ops/observability/runbooks/degradation-critical.md` | Runbook: DegradationCritical |
| `ops/observability/runbooks/autonomy-violation.md` | Runbook: AutonomyViolation |
| `ops/observability/runbooks/cloud-budget-exceeded.md` | Runbook: CloudBudgetExceeded |
| `ops/observability/runbooks/handler-lag.md` | Runbook: HandlerLag |
| `ops/observability/runbooks/rollback-occurred.md` | Runbook: RollbackOccurred |
| `ops/observability/runbooks/exporter-drops.md` | Runbook: ExporterDropping |
| `ops/observability/runbooks/synthetic-failing.md` | Runbook: SyntheticProbeFailing |
| `tests/telemetry/logger.test.ts` | Log formatting, rotation, level filtering |
| `tests/telemetry/metrics.test.ts` | Instrument registry, export format, exemplar attachment |
| `tests/telemetry/projector.test.ts` | Event → signal derivation; union exhaustiveness |
| `tests/telemetry/gauges.test.ts` | Gauge callbacks query DB, observe correct values |
| `tests/telemetry/dashboards.test.ts` | All dashboard JSON parses and references valid metric names |
| `tests/telemetry/labels.test.ts` | Closed-set guard rejects unknown values; every metric's label enums are complete |
| `tests/telemetry/redact.test.ts` | Allowlist enforced; body/content never appear in exported attributes; redaction counter increments |
| `tests/telemetry/context.test.ts` | Handler span is a child of emitter span across bus dispatch; traceId round-trips through event metadata |
| `tests/telemetry/semconv.test.ts` | Span attributes follow OTel semantic conventions (lint over spans/*.ts) |
| `tests/telemetry/resource.test.ts` | Resource attributes include `service.name`, `service.version` (git sha), `service.instance.id` |
| `tests/telemetry/sampling.test.ts` | Tail sampler keeps traces with errors; samples success traces at configured rate |
| `tests/telemetry/exporter_backpressure.test.ts` | Exporter queue saturation drops and counts; main loop is not blocked when collector is unreachable |
| `tests/telemetry/slos.test.ts` | SLI definitions compile against metric registry; burn-rate windows are valid |
| `tests/telemetry/synthetic.test.ts` | Prober issues a canary turn and records probe metrics |
| `tests/telemetry/alerts.test.ts` | Every alert rule has a `runbook_url`; every runbook file exists; Prom/Loki expressions parse |
| `tests/telemetry/recording_rules.test.ts` | Recording rule expressions reference valid metrics and parse |
| `tests/selfupdate/healthcheck.test.ts` | Check pass/fail, commit tracking |
| `tests/selfupdate/rollback.test.ts` | Rollback execution, event emission |

### Files to modify

| File | Change |
| ---- | ------ |
| `src/engine.ts` | Build `TelemetryBundle` at startup; run healthcheck |
| `src/bus/dispatch.ts` | Wrap handler invocation with `spans/bus.ts` + duration/error metrics |
| `src/chat/engine.ts` | Receive `withSpan` via DI; wrap turn at the top boundary only |
| `src/memory/retrieval.ts` | Receive `withSpan`; wrap RRF at the top boundary only |
| `src/db/connect.ts` | Apply postgres.js query hook from `spans/db.ts` |
| `src/events/types.ts` | Add `system.rollback`, `system.degradation.healed` to SystemEvent union |
| `biome.json` | `noRestrictedImports`: block `@opentelemetry/*` and `src/telemetry/*` outside `src/telemetry/**` and `src/engine.ts` |
| `justfile` | Add `just observe-up` / `just observe-down` for the local LGTM stack |

## Design decisions

### Workspace layout

```text
~/Theo/                          — workspace root (configurable via THEO_WORKSPACE)
├── logs/                        — structured log files (daily rotation)
│   ├── theo-2026-04-06.log
│   └── theo-2026-04-05.log.gz
├── data/                        — local data (embeddings cache, healthy_commit)
│   └── healthy_commit           — SHA of last known-good commit
└── config/                      — runtime overrides (optional)
    └── overrides.json
```

The workspace is separate from the source code repository. Source lives in its own clone (e.g.,
`~/Code/theo`). This separation means a broken source update doesn't corrupt operational state.
Observability container volumes live under `ops/observability/data/` inside the repo (ignored by
git) so they're independent of both the workspace and the source tree.

### Telemetry module architecture

The module exposes a single bundle:

```typescript
// src/telemetry/index.ts
export interface TelemetryBundle {
  readonly withSpan: WithSpan;                  // the ONLY helper business code sees
  readonly shutdown: () => Promise<void>;        // flush exporters on engine stop
}

export async function initTelemetry(
  config: TelemetryConfig,
  bus: EventBus,
  db: Sql,
): Promise<TelemetryBundle> {
  const logger = new TheoLogger(config);
  const tracer = initTracer(config);             // OTLP or no-op
  const metrics = initMetrics(config);           // OTLP or log-based fallback
  const projector = new TelemetryProjector({ logger, metrics });

  bus.subscribe(projector.handleEvent);          // events → signals
  registerGauges({ metrics, db });               // periodic DB pulls
  installBusSpan(bus, tracer);                   // wrap handler dispatch
  installDbSpan(db, metrics);                    // postgres.js query hook

  return {
    withSpan: tracer.withSpan,
    shutdown: async () => { await tracer.shutdown(); await metrics.shutdown(); },
  };
}
```

Only `withSpan` leaks out of the module. The logger and metrics registry are internal; the
projector and `registerGauges` are the only writers.

### Event-driven observability (the projector)

The projector is a bus handler. It pattern-matches on event type and emits signals. It is
exhaustive over the `DomainEvent` union — a missing case is a compile error, which prevents
silent observability gaps.

```typescript
// src/telemetry/projector.ts
export class TelemetryProjector {
  constructor(private readonly deps: ProjectorDeps) {}

  handleEvent = async (event: DomainEvent): Promise<void> => {
    const m = this.deps.metrics;
    switch (event.type) {
      case "turn.completed":
        m.turnCounter.add(1, { gate: event.data.gate });
        m.turnDuration.record(event.data.durationMs, { gate: event.data.gate });
        for (const it of event.data.usage.iterations) {
          m.inputTokens.add(it.tokens.input, { model: it.model, role: it.role });
          m.outputTokens.add(it.tokens.output, { model: it.model, role: it.role });
          m.costCounter.add(it.costUsd, { model: it.model, role: it.role });
        }
        return;

      case "proposal.expired":
        m.proposalsExpired.add(1, { kind: event.data.kind });
        return;

      case "autonomy.violation":
        m.autonomyViolations.add(1, { domain: event.data.domain });
        this.deps.logger.warn("autonomy violation", { ...event.data });
        return;

      // ... every other DomainEvent case ...

      default: {
        const _exhaustive: never = event;
        return _exhaustive;
      }
    }
  };
}
```

Three rules govern the projector:

1. **Exhaustive over `DomainEvent`.** The `never` default enforces it.
2. **No side effects beyond emission.** No DB writes, no external calls.
3. **Duration/cost data lives in the event, not the projector.** The emitter measures; the
   projector merely records. This keeps the event log self-describing.

The projector is replay-safe: counters accumulate deltas as events flow, gauges are pulled (not
pushed) from the DB, so replay does not double-count.

### Integration surfaces

**1. Event emission (preferred).** Business code emits domain events — already its job.

**2. `withSpan` at boundaries.** Only four call sites:

```typescript
// src/chat/engine.ts
async function handleMessage(body: string, gate: Gate): Promise<TurnResult> {
  return deps.withSpan("turn", { gate, "message.length": body.length }, async () => {
    const ctx = await assembleContext(deps, body);
    const result = await runSdkQuery(deps, ctx);
    await deps.bus.emit({ type: "turn.completed", data: { ... } });
    return result;
  });
}
```

- `chat.engine.handleMessage` → `turn` root span
- `memory.retrieval.rrfSearch` → `retrieval.rrf` child span
- SDK wrapper → `sdk.query` child span
- `bus.dispatch(handler, event)` → one span per handler invocation (auto-applied, not per-handler code)

Child spans from lower-level `withSpan` calls nest automatically via OTel's active-context
propagation.

**3. Observable gauges.** All in one file:

```typescript
// src/telemetry/gauges.ts
export function registerGauges({ metrics, db }: GaugeDeps): void {
  metrics.nodesGauge.addCallback(async (r) => {
    const [{ count }] = await db`SELECT count(*)::int FROM node`;
    r.observe(count);
  });
  metrics.goalsActive.addCallback(async (r) => {
    const [{ count }] = await db`SELECT count(*)::int FROM goal WHERE state = 'active'`;
    r.observe(count);
  });
  metrics.proposalsPending.addCallback(async (r) => {
    const [{ count }] = await db`SELECT count(*)::int FROM proposal WHERE state = 'pending'`;
    r.observe(count);
  });
  metrics.handlerLag.addCallback(async (r) => {
    const rows = await db`
      SELECT handler, EXTRACT(EPOCH FROM now() - last_checkpoint_at) AS lag
      FROM handler_checkpoint
    `;
    for (const { handler, lag } of rows) r.observe(lag, { handler });
  });
  // ... all other DB-backed gauges
}
```

Pull frequency is 60 s, adjustable via `TelemetryConfig`.

### Cardinality discipline

Every label on every metric draws values from a **closed-set enum** declared in
`src/telemetry/labels.ts`. A value that isn't in the enum is rejected by `assertLabel`,
replaced with `unknown`, and counted via `theo.telemetry.cardinality_rejections_total{metric,
label}`. This prevents Prometheus OOMs from a free-form `reason` field or an unbounded
`handler` set.

```typescript
// src/telemetry/labels.ts
export const GATES = ["telegram.owner", "cli.owner", "webhook.reflex", "internal.scheduler"] as const;
export const MODELS = ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"] as const;
export const ROLES = ["executor", "advisor", "ideation"] as const;
export const HANDLER_ERROR_REASONS = [
  "db_error", "validation_error", "upcaster_error", "timeout", "unknown",
] as const;
export const REFLEX_REJECT_REASONS = [
  "signature_invalid", "source_denied", "rate_limited", "schema_invalid", "unknown",
] as const;
export const AUTONOMY_DOMAINS = [
  "git_write", "github_api", "cloud_api", "filesystem", "network", "shell",
] as const;
// ... one enum per label

export function assertLabel<T extends readonly string[]>(
  enumSet: T,
  value: string,
  metric: string,
  label: string,
): T[number] | "unknown" {
  if ((enumSet as readonly string[]).includes(value)) return value as T[number];
  metrics.cardinalityRejections.add(1, { metric, label });
  return "unknown";
}
```

`tests/telemetry/labels.test.ts` asserts every label used in the projector and span wrappers
is guarded by `assertLabel`, and that every enum's value set is complete relative to the
domain event union. Adding a new reason requires a code change and a review — not a free-form
string.

### OTel semantic conventions

Wherever an OTel semantic convention exists, Theo uses it. `src/telemetry/semconv.ts`
re-exports the constants used across the module so there's a single source of truth:

```typescript
// src/telemetry/semconv.ts
export const ATTR_DB_SYSTEM = "db.system";            // "postgresql"
export const ATTR_DB_OPERATION = "db.operation";       // "SELECT" | "INSERT" | ...
export const ATTR_DB_STATEMENT = "db.statement";       // redacted to operation + table
export const ATTR_MESSAGING_SYSTEM = "messaging.system";         // "theo.eventbus"
export const ATTR_MESSAGING_DESTINATION = "messaging.destination.name"; // event type
export const ATTR_MESSAGING_OP = "messaging.operation";          // "publish" | "receive"
export const ATTR_CODE_FUNCTION = "code.function";
export const ATTR_CODE_NAMESPACE = "code.namespace";
export const ATTR_SERVICE_NAME = "service.name";
export const ATTR_SERVICE_VERSION = "service.version";
export const ATTR_SERVICE_INSTANCE_ID = "service.instance.id";
export const ATTR_DEPLOYMENT_ENVIRONMENT = "deployment.environment";
// Theo-specific attributes live under theo.* namespace
export const ATTR_THEO_GATE = "theo.gate";
export const ATTR_THEO_GOAL_ID = "theo.goal.id";
export const ATTR_THEO_MODEL = "theo.model";
export const ATTR_THEO_ROLE = "theo.role";
```

`tests/telemetry/semconv.test.ts` lints the `spans/*.ts` files and fails if any span uses a
non-semconv attribute name for a domain that has a convention (DB, messaging, code location).

### Resource attributes

The SDK is bootstrapped with a resource that tags every span, metric, log, and profile:

```typescript
// src/telemetry/resource.ts
export async function buildResource(config: TelemetryConfig): Promise<Resource> {
  const commit = (await Bun.$`git rev-parse HEAD`.text()).trim();
  const hostname = os.hostname();
  return resourceFromAttributes({
    [ATTR_SERVICE_NAME]: "theo",
    [ATTR_SERVICE_VERSION]: commit,
    [ATTR_SERVICE_INSTANCE_ID]: hostname,
    [ATTR_DEPLOYMENT_ENVIRONMENT]: config.environment,      // "prod" | "dev" | "test"
    "process.runtime.name": "bun",
    "process.runtime.version": Bun.version,
    "host.os.type": process.platform,
    "host.arch": process.arch,
  });
}
```

Self-update changes `service.version` at restart, so regressions can be correlated to commits
without instrumentation code changes.

### Exemplars and cross-signal correlation

Every histogram observation carries an exemplar containing the active `traceId`. In Grafana,
clicking a p99 outlier jumps straight to the offending trace. Enable via
`PeriodicExportingMetricReader` with exemplar-enabled aggregation.

```typescript
// src/telemetry/metrics.ts (initMetrics sketch)
const reader = new PeriodicExportingMetricReader({
  exporter: otlpMetricExporter,
  exportIntervalMillis: 10_000,
  aggregationSelector: (instrumentType) => defaultAggregationWithExemplars(instrumentType),
});
```

`tests/telemetry/metrics.test.ts` includes an assertion that a histogram observation made
inside an active span exports an exemplar carrying that `traceId`.

### Context propagation across async boundaries

Events are async: a `turn.completed` emitted inside a turn span may be processed by a handler
seconds later. Without explicit propagation, the handler's span would be an orphan. Theo's
bus wraps emit and dispatch to round-trip context through the event:

```typescript
// src/telemetry/context.ts
export function injectContext(metadata: EventMetadata): EventMetadata {
  const active = trace.getActiveSpan()?.spanContext();
  if (!active) return metadata;
  return {
    ...metadata,
    traceId: active.traceId,
    spanId: active.spanId,
    traceFlags: active.traceFlags,
  };
}

export function rehydrateContext(metadata: EventMetadata): Context {
  if (!metadata.traceId || !metadata.spanId) return context.active();
  const spanContext = {
    traceId: metadata.traceId,
    spanId: metadata.spanId,
    traceFlags: metadata.traceFlags ?? 1,
    isRemote: true,
  };
  return trace.setSpanContext(context.active(), spanContext);
}
```

The bus's `emit` calls `injectContext`; the dispatcher wraps each handler invocation with
`context.with(rehydrateContext(event.metadata), () => handler(event))`. Replay is handled by
the same path — replayed events rehydrate their original `traceId`, so a goal's entire history
is reconstructible as a single trace timeline.

`tests/telemetry/context.test.ts` seeds a span, emits an event, dispatches to a handler, and
asserts the handler's span is a child of the seed span.

### Sampling strategy

- **Traces.** Tail-based sampling in the OTel Collector: keep 100% of traces with any span
  marked `status=error` or any span duration > 10 s; sample the remainder at 20%. Personal
  agent scale makes 100% affordable in absolute terms but budgets are set to survive future
  growth.
- **Logs.** `info` level by default; `debug` enabled per-component via config overrides.
  Promtail drops debug logs older than 7 days before shipping — they stay on disk for
  post-mortem.
- **Metrics.** No sampling; Prometheus scrapes at 15 s intervals.

Sampling policy is defined once in `src/telemetry/sampling.ts` and in the collector config.
`tests/telemetry/sampling.test.ts` exercises both success and error paths.

### Structured logging

Every log entry is a JSON line:

```typescript
interface LogEntry {
  readonly timestamp: string;     // ISO 8601
  readonly level: "debug" | "info" | "warn" | "error";
  readonly message: string;
  readonly traceId?: string;
  readonly spanId?: string;
  readonly component: string;
  readonly attributes: Record<string, unknown>;
}
```

The logger writes to two destinations:

1. **File** — daily-rotated files in `~/Theo/logs/`. Files older than 30 days are gzipped;
   older than 90 days are deleted.
2. **stdout** — for `launchd` to capture and for development.

Promtail (part of the observability stack) tails the log directory and ships entries to Loki.
This means the filesystem is the source of truth; Loki is a projection. If the observability
stack is down, logs still exist on disk.

The logger is a telemetry-module implementation detail. Outside `src/telemetry/`, nothing imports
it. Business code that feels tempted to log directly should emit an event — the projector turns
it into a log entry if the event type warrants one.

### OTel tracing

The span tree emerges from three sources:

```text
turn (root span, from chat.engine)
├── retrieval.rrf (child, from memory.retrieval)
├── sdk.query (child, from sdk wrapper)
│   └── tool.* (child, per tool call inside SDK)
├── bus.handler.<name> (child, auto-applied per handler invocation)
│   ├── auto_edges
│   └── contradiction_check
└── db.query (child, for queries above latency threshold)
```

Tracing uses `@opentelemetry/api` for span creation and context propagation. The exporter is
configurable:

- **OTLP exporter** — when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, traces export to the collector
  via a `BatchSpanProcessor` with a bounded queue (see **Collector resilience**).
- **No-op** — when no collector is configured, tracing is a no-op. Spans still carry
  `traceId`/`spanId` into log entries for correlation, but no network traffic.

Trace context propagates across async boundaries via `src/telemetry/context.ts` (see above):
emitted events carry `metadata.traceId`/`spanId`; the bus dispatcher rehydrates the context
before invoking handlers. Spans follow OTel semantic conventions (see above). Every histogram
observation inside a span carries an exemplar with the `traceId`.

### Metrics

Metric instruments are defined once in `src/telemetry/metrics.ts` and held on a
`MetricsRegistry` object. The projector and `registerGauges` are the only call sites that
interact with instruments.

```typescript
// src/telemetry/metrics.ts
export function initMetrics(config: TelemetryConfig): MetricsRegistry {
  const meter = metrics.getMeter("theo");
  return {
    turnCounter: meter.createCounter("theo.turns.total"),
    turnDuration: meter.createHistogram("theo.turns.duration_ms"),
    inputTokens: meter.createCounter("theo.tokens.input"),
    outputTokens: meter.createCounter("theo.tokens.output"),
    costCounter: meter.createCounter("theo.cost.usd"),
    // ... all other instruments from the cross-cutting table
    handlerDuration: meter.createHistogram("theo.bus.handler_duration_ms"),
    handlerErrors: meter.createCounter("theo.bus.handler_errors_total"),
    handlerLag: meter.createObservableGauge("theo.bus.handler_lag_seconds"),
    nodesGauge: meter.createObservableGauge("theo.memory.nodes_gauge"),
    goalsActive: meter.createObservableGauge("theo.goals.active_gauge"),
    // ...
  };
}
```

Adding a metric is a three-step change:

1. Define the instrument in `metrics.ts`.
2. Emit or observe it in `projector.ts` or `gauges.ts`.
3. Add a panel to the relevant dashboard JSON.

No business code changes.

### PII and secrets scrubbing

Theo is a *personal* agent. Message bodies, memory contents, tool arguments, and proposal
payloads all contain sensitive data that must never leave the process in span attributes or
log entries. Scrubbing is enforced by a **redaction span processor** and a **redaction log
processor**, both sourced from a single allowlist.

```typescript
// src/telemetry/redact.ts
export const ATTR_ALLOWLIST: ReadonlySet<string> = new Set([
  // OTel semconv (DB, messaging, code, service, host) — full prefix match
  "db.system", "db.operation", "messaging.system", "messaging.destination.name",
  "code.function", "code.namespace", "service.*", "host.*",
  // Theo-specific: ids, enums, counts, durations — NEVER content
  "theo.gate", "theo.model", "theo.role", "theo.goal.id", "theo.proposal.id",
  "theo.event.id", "theo.event.type", "theo.event.version",
  "theo.message.length",        // length only; body never
  "theo.tokens.input", "theo.tokens.output",
  "theo.cost.usd",
  "theo.autonomy.domain",
  "theo.degradation.level",
]);

export class RedactionSpanProcessor implements SpanProcessor {
  onEnd(span: ReadableSpan): void {
    for (const key of Object.keys(span.attributes)) {
      if (!isAllowed(key)) {
        span.attributes[key] = "[redacted]";
        metrics.redactions.add(1, { key: coarsen(key) });
      }
    }
  }
}
```

Rules:

- **Allowlist, not blocklist.** Anything not explicitly permitted is redacted. New attributes
  require a review.
- **No content, ever.** Message bodies, tool arguments, memory node text, proposal content —
  represented as length, hash, or id only.
- **DB statements are coarsened.** `db.statement` is set to `"SELECT FROM node WHERE ..."` —
  the operation and table, never bound parameters.
- **Logs go through the same allowlist.** `TheoLogger` filters `attributes` before writing.

`tests/telemetry/redact.test.ts` runs the full span tree of a real turn through the processor
and asserts that no exported attribute contains known-sensitive tokens (configurable test
fixtures: "secret123", "ssn", "owner@example"). Also asserts `redactions_total` increments
when a disallowed key is emitted.

### Collector resilience and backpressure

The SDK's `BatchSpanProcessor` and `PeriodicExportingMetricReader` are configured with
**bounded queues** and explicit drop semantics. A collector outage must not block the main
loop.

```typescript
// src/telemetry/exporters.ts
const spanProcessor = new BatchSpanProcessor(otlpExporter, {
  maxQueueSize: 2048,              // drop beyond this
  maxExportBatchSize: 512,
  scheduledDelayMillis: 5_000,
  exportTimeoutMillis: 10_000,
});

// Wrap the processor to count drops
class CountingSpanProcessor implements SpanProcessor {
  constructor(private inner: BatchSpanProcessor, private onDrop: () => void) {}
  onEnd(span: ReadableSpan): void {
    if (this.inner.queueSize >= this.inner.maxQueueSize) {
      this.onDrop();
      return;
    }
    this.inner.onEnd(span);
  }
}
```

Drops increment `theo.telemetry.exporter_dropped_total{signal}`; queue fill ratio is a gauge.
An alert (`ExporterDropping`) fires if drops exceed a threshold for 10 minutes — the
observability stack is either wedged or undersized.

`tests/telemetry/exporter_backpressure.test.ts` points the exporter at a black-hole endpoint
and asserts: (1) the main loop completes 100 turns in bounded time, (2) spans are dropped and
counted once the queue fills, (3) the process never throws or hangs.

### Recording rules and retention tiers

**Recording rules** (`ops/observability/prometheus/recording_rules.yaml`) precompute
expensive aggregations so dashboards and alerts read cheap time series:

```yaml
groups:
  - name: theo_turns
    interval: 30s
    rules:
      - record: theo:turns:p95_1h
        expr: histogram_quantile(0.95, sum by (le, gate) (rate(theo_turns_duration_ms_bucket[1h])))
      - record: theo:turns:error_rate_5m
        expr: sum(rate(theo_turns_failed_total[5m])) / sum(rate(theo_turns_total[5m]))
  - name: theo_slo
    interval: 30s
    rules:
      - record: theo:slo:turns_available:burn_rate_5m
        expr: (1 - theo:turns:error_rate_5m) < bool 0.99
```

**Retention tiers**:

| Signal | Hot | Cold | Total |
| ------ | --- | ---- | ----- |
| Logs (on disk) | 30d uncompressed | 60d gzipped | 90d |
| Logs (Loki) | 30d | — | 30d |
| Metrics (Prometheus) | 30d raw | 180d downsampled (Mimir/Thanos if configured) | 180d |
| Traces (Tempo) | 14d | — | 14d |
| Profiles (Pyroscope) | 14d | — | 14d |

All retentions are configured in the respective component configs; defaults are not inherited.

### Grafana stack (local LGTM)

A local stack runs in `docker compose` alongside the existing Postgres container:

| Component | Role | Port |
| --------- | ---- | ---- |
| OTel Collector | Receives OTLP from Theo, tail-sampling, fans out to backends; exposes its own metrics | 4317 (gRPC), 4318 (HTTP), 8888 (self) |
| Prometheus | Metrics storage (scrapes Theo, Collector, itself, Loki, Tempo, Pyroscope) | 9090 |
| Loki | Log storage | 3100 |
| Promtail | Tails `~/Theo/logs/` into Loki | — |
| Tempo | Trace storage | 3200 |
| Pyroscope | Continuous profiles (CPU, heap) | 4040 |
| Grafana | Unified UI + alerting | 3000 |

Pipeline:

```text
Theo process
  ├── metrics   ──OTLP──▶ Collector ──▶ Prometheus (remote-write)
  ├── traces    ──OTLP──▶ Collector ──(tail sampling)──▶ Tempo
  ├── profiles  ──HTTP──▶ Pyroscope
  └── logs      ──file──▶ Promtail  ──▶ Loki
                                              │
                                          Grafana
                                           datasources:
                                             Prom, Tempo, Loki, Pyroscope, Postgres
```

Theo exports when `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` is set. Grafana's
file-based provisioning loads datasources, dashboards, alert rules, and contact points from the
in-repo files on startup.

**Alternative — Grafana Cloud.** Same OTLP pipeline, different endpoint + auth headers. For
users wanting zero local infrastructure this is the supported zero-infra path. Dashboards and
alerts are provisioned via Grafana Cloud's Terraform or API (future work; v1 documents the
manual import path).

`just observe-up` and `just observe-down` manage the compose lifecycle.

### Dashboards as code

Dashboards live in `ops/observability/grafana/dashboards/` as JSON:

| Dashboard | Focus |
| --------- | ----- |
| `overview.json` | One-page health: up, degradation level, last turn, pending proposals, cost today |
| `cost.json` | Executor vs advisor vs cloud egress; per-gate, per-model, daily/weekly trends |
| `turns.json` | Turn rate, latency p50/p95/p99 by gate, token usage, retrieval latency |
| `goals.json` | Active/quarantined counts, task-turn success, lease contention |
| `proposals.json` | Pending count, approval/expiry rate, time-to-approval, by kind |
| `reflex.json` | Webhooks received, rejected (by reason), rate-limited, dispatched turns |
| `autonomy.json` | Violations by domain, degradation level history, healing events |
| `bus.json` | Handler latency, lag, error rate, events appended by type |
| `events.json` | Event browser (Postgres datasource; raw shape — not upcast) |

Workflow: edit in Grafana locally → export JSON → commit. `tests/telemetry/dashboards.test.ts`
parses every dashboard JSON and verifies that every `expr` references a metric name that exists
in `MetricsRegistry`. This catches dashboards drifting from the metric set at PR time instead of
at runtime.

### SLOs, burn-rate alerts, and error budget

Alerting is anchored on **SLOs with multi-window multi-burn-rate** alerts (per Google SRE
workbook), not raw thresholds. Thresholds remain for binary conditions (up/down, violation
occurred) but latency and availability are SLO-driven.

SLIs and SLOs (`src/telemetry/slos.ts`):

| SLO | SLI | Target | Window |
| --- | --- | ------ | ------ |
| `turn_available` | ratio of `theo.turns.total{status=ok}` to total | 99.0% | 30d rolling |
| `turn_latency` | ratio of turn durations ≤ 30s | 95.0% | 30d rolling |
| `retrieval_latency` | ratio of `retrieval.duration_ms` ≤ 2s | 99.0% | 30d rolling |
| `reflex_available` | ratio of reflex dispatches ≤ 5s after webhook receipt | 99.0% | 30d rolling |
| `proposal_freshness` | ratio of proposals resolved before expiry | 90.0% | 30d rolling |

Each SLO generates a **fast-burn** alert (page) and a **slow-burn** alert (ticket) via
recording rules:

| Alert | Windows | Burn rate | Severity |
| ----- | ------- | --------- | -------- |
| `SLOFastBurn_<slo>` | 1h and 5m | > 14.4 (2% of 30d budget in 1h) | critical |
| `SLOSlowBurn_<slo>` | 6h and 30m | > 6 (5% of 30d budget in 6h) | warning |

Binary alerts (still required for non-SLO conditions):

| Alert | Fires when | Severity |
| ----- | ---------- | -------- |
| `TheoDown` | No samples from `theo.turns.total` for 15 min AND no reflex activity | critical |
| `DegradationElevated` | `theo.degradation.level_gauge >= 2` for 10 min | warning |
| `DegradationCritical` | `theo.degradation.level_gauge >= 4` for 5 min | critical |
| `AutonomyViolation` | `rate(theo_autonomy_violations_total[5m]) > 0` | critical |
| `CloudBudgetNearLimit` | Daily spend > 80% of budget | warning |
| `CloudBudgetExceeded` | Daily spend > 100% of budget | critical |
| `HandlerLag` | `theo_bus_handler_lag_seconds > 300` for 10 min | warning |
| `ProposalBacklog` | `theo_proposals_pending_gauge > 20` for 1h | warning |
| `RollbackOccurred` | `count_over_time(system_rollback[1h]) > 0` | critical |
| `ExporterDropping` | `rate(theo_telemetry_exporter_dropped_total[10m]) > 0` | warning |
| `SyntheticProbeFailing` | `rate(theo_synthetic_probe_failures_total[15m]) > 0` | critical |
| `CardinalityRejections` | `rate(theo_telemetry_cardinality_rejections_total[1h]) > 0` | warning |

**Error budget policy.** When `theo:slo:error_budget_remaining_ratio{slo=...}` drops below
0.1, the `SelfUpdateBlocked` label gate fires: Theo must not auto-merge self-update PRs until
the SLO recovers. This is enforced in the self-update path by querying Prometheus before
merge.

Contact points (`contactpoints.yaml`): **Telegram** (reuses the gate's bot + owner chat ID)
and **email**. Routing policy (`policies.yaml`) sends critical alerts to both; warnings to
Telegram only; includes **silence/maintenance windows** for planned self-update sessions.
Alert state is durable across Grafana restarts.

Every alert rule carries a `runbook_url` annotation pointing to a file under
`ops/observability/runbooks/`. A test (`tests/telemetry/alerts.test.ts`) fails CI if any
alert rule is missing a runbook URL or if the referenced file does not exist.

### Runbooks as code

Every alert has a runbook Markdown file with a fixed structure:

```markdown
# <AlertName>

## What it means
<One paragraph on the user-visible symptom.>

## Triage
1. <First thing to check — usually a Grafana link>
2. <Second check>

## Resolution
- **If X:** <fix>
- **If Y:** <fix>

## Related
- Dashboard: <link>
- Source: <file:line>
```

A test asserts every runbook file parses and contains all four headings.

### Continuous profiling (Pyroscope)

A Pyroscope client in `src/telemetry/profiling.ts` captures heap and CPU profiles every 15 s
and ships to the Pyroscope backend. This is critical for a long-running self-modifying
process: heap growth across self-update commits is the class of bug that metrics miss.

```typescript
// src/telemetry/profiling.ts
import Pyroscope from "@pyroscope/nodejs";

export function initProfiling(config: TelemetryConfig): void {
  if (!config.pyroscope.enabled) return;
  Pyroscope.init({
    serverAddress: config.pyroscope.endpoint,
    appName: "theo",
    tags: { version: config.gitSha, instance: os.hostname() },
  });
  Pyroscope.start();
}
```

Grafana's Pyroscope datasource is wired into the `profiling.json` dashboard and into trace
exemplar links (click a slow span → flame graph at that moment).

### Synthetic health check

A prober in `src/telemetry/synthetic.ts` sends a canary turn every 5 minutes via the
`internal.scheduler` gate and verifies a response. Detects "alive but stuck" — the failure
mode where `launchd` still sees a live process but Theo can't actually produce a turn.

```typescript
// src/telemetry/synthetic.ts
export async function runProbe(deps: ProbeDeps): Promise<void> {
  const start = performance.now();
  try {
    const result = await withTimeout(
      deps.chat.handleMessage("ping", "internal.scheduler"),
      30_000,
    );
    if (!result.ok) throw new Error("turn returned not ok");
    metrics.probeDuration.record(performance.now() - start);
  } catch (err) {
    metrics.probeFailures.add(1, { reason: classifyError(err) });
    deps.logger.warn("synthetic probe failed", { error: String(err) });
  }
}
```

The prober is scheduled by the existing scheduler (Phase 12) and produces a dedicated event
(`synthetic.probe.completed`) so probe results appear in the event log and are replayable.
Probe turns are tagged so they don't pollute cost dashboards for real user activity.

### Meta-observability

The observability stack observes itself. Prometheus scrapes its own `/metrics`, the
Collector's `:8888/metrics`, Loki's metrics, Tempo's metrics, and Pyroscope's metrics. A
`meta.json` dashboard surfaces:

- Collector: receive/export rates, queue depths, refused items
- Prometheus: WAL size, query latency, scrape errors
- Loki: ingestion rate, chunk storage, query latency
- Tempo: ingester bytes, query latency
- Pyroscope: ingestion rate, storage

Alerts on the meta layer:

| Alert | Fires when | Severity |
| ----- | ---------- | -------- |
| `CollectorRefusing` | `otelcol_receiver_refused_spans > 0` for 10 min | warning |
| `PromScrapeFailing` | `up{job="theo"} == 0` for 5 min | critical |
| `LokiIngestErrors` | `rate(loki_request_duration_seconds_count{status_code!~"2.."}[5m]) > 0` | warning |

### Future: typed domain views (deferred)

A small read-only custom UI (events through the upcaster, goal inspector, proposal queue,
memory graph explorer) would address the upcaster-fidelity gap and render typed domain
aggregates that don't fit dashboards. This is **deferred**, not scoped, for Phase 15 — Grafana
is the only observability UI now. Add it when the raw-events limitation bites in practice.

### Always running via launchd

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.theo.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/owner/.bun/bin/bun</string>
    <string>run</string>
    <string>src/index.ts</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/owner/Code/theo</string>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/Users/owner/Theo/logs/launchd-stdout.log</string>
  <key>StandardErrorPath</key><string>/Users/owner/Theo/logs/launchd-stderr.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>THEO_WORKSPACE</key><string>/Users/owner/Theo</string>
    <key>OTEL_EXPORTER_OTLP_ENDPOINT</key><string>http://localhost:4318</string>
  </dict>
  <key>ThrottleInterval</key><integer>10</integer>
</dict>
</plist>
```

`ThrottleInterval: 10` prevents rapid restart loops. The engine's startup sequence (migrations →
healthcheck → bus replay → scheduler → gate → views) handles recovery on restart.

### Self-update safety

```typescript
interface HealthCheckResult {
  readonly ok: boolean;
  readonly commit: string;
  readonly healthyCommit: string;
  readonly errors?: string[];
}

async function runHealthCheck(workspace: string): Promise<HealthCheckResult> {
  const commit = (await Bun.$`git rev-parse HEAD`.text()).trim();
  const healthyCommit = await readHealthyCommit(workspace);
  const check = await Bun.$`just check`.quiet().nothrow();

  if (check.exitCode === 0) {
    await writeHealthyCommit(workspace, commit);
    return { ok: true, commit, healthyCommit: commit };
  }
  return { ok: false, commit, healthyCommit, errors: [check.stderr.toString()] };
}

async function rollbackToHealthy(workspace: string, bus: EventBus): Promise<void> {
  const healthy = await readHealthyCommit(workspace);
  if (!healthy) throw new Error("No healthy commit recorded — cannot rollback");
  const from = (await Bun.$`git rev-parse HEAD`.text()).trim();
  await Bun.$`git reset --hard ${healthy}`;
  await bus.emit({
    type: "system.rollback",
    version: 1,
    actor: "system",
    data: { fromCommit: from, toCommit: healthy, reason: "healthcheck_failed" },
    metadata: {},
  });
}
```

**Startup sequence:**

```text
Engine.start()
  → run migrations
  → run healthcheck
     → if FAIL: rollback to healthy_commit, exit (launchd restarts us)
     → if PASS: update healthy_commit
  → init telemetry (resource → tracer → meter → projector → gauges → profiling → synthetic)
  → start bus (replay from checkpoints — projector processes replay; context rehydrates)
  → start scheduler (registers synthetic probe as a periodic job)
  → start gate
```

Pre-merge SLO gate (inside `self-update`): before `gh pr merge --auto`, Theo queries
Prometheus for `theo:slo:error_budget_remaining_ratio` for every SLO. If any budget is below
10%, the merge is blocked with a `SelfUpdateBlocked` event.

**Branch discipline for self-updates:**

- Theo commits to feature branches, never directly to main
- Opens PRs via `gh pr create`
- Auto-merges when `just check` passes on the branch
- `healthy_commit` always points to a main commit that passed checks

### Install script

```bash
#!/bin/bash
# ops/install.sh — one-shot setup for Theo on macOS

WORKSPACE="${THEO_WORKSPACE:-$HOME/Theo}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$WORKSPACE/logs" "$WORKSPACE/data" "$WORKSPACE/config"
git -C "$REPO_DIR" rev-parse HEAD > "$WORKSPACE/data/healthy_commit"

PLIST="$HOME/Library/LaunchAgents/com.theo.agent.plist"
sed "s|/Users/owner|$HOME|g; s|/Users/owner/Code/theo|$REPO_DIR|g" \
  "$REPO_DIR/ops/com.theo.agent.plist" > "$PLIST"
launchctl load "$PLIST"

if [ "$1" = "--with-observability" ]; then
  (cd "$REPO_DIR/ops/observability" && docker compose up -d)
  echo "Grafana: http://localhost:3000 (admin/admin)"
fi

echo "Theo installed. Workspace: $WORKSPACE"
echo "Logs: $WORKSPACE/logs/"
echo "Stop: launchctl unload $PLIST"
```

## Definition of Done

### Framework discipline

- [ ] `src/telemetry/` is the single import site for `@opentelemetry/*`; biome rule enforces it
- [ ] `TelemetryBundle` exposes only `withSpan` + `shutdown` to the rest of the codebase
- [ ] `TelemetryProjector.handleEvent` is exhaustive over `DomainEvent` (compile-enforced)
- [ ] `registerGauges` is the only DB-backed observable gauge site
- [ ] Bus dispatch auto-wraps each handler with a span + duration metric + error metric
- [ ] Postgres.js query-timing hook emits `theo.db.query_duration_ms` with `db.*` semconv

### Signals and correlation

- [ ] `MetricsRegistry` defines every instrument in the cross-cutting table
- [ ] Resource attributes `service.name`, `service.version=<git sha>`, `service.instance.id`,
      `deployment.environment` attached to every span, metric, log, profile
- [ ] OTel semantic conventions applied wherever defined (DB, messaging, code) — enforced by
      `semconv.test.ts`
- [ ] Every histogram observation inside a span carries a `traceId` exemplar
- [ ] Events carry `metadata.traceId`/`spanId`; handler dispatch rehydrates context so
      handler spans nest under emitter
- [ ] Structured logs carry `traceId`/`spanId` from the active context

### Cardinality, sampling, retention

- [ ] Every metric label value is drawn from a closed-set enum in `src/telemetry/labels.ts`;
      `assertLabel` guards every emission site
- [ ] `cardinality_rejections_total` increments on unknown values; `labels.test.ts` asserts
      completeness
- [ ] Tail-based sampling in the collector: 100% of error/slow traces, 20% of success
- [ ] Log retention: 30d file uncompressed, 60d gzipped, 90d total; Loki 30d
- [ ] Metric retention 30d raw (180d downsampled if Mimir/Thanos configured); trace
      retention 14d; profile retention 14d
- [ ] Recording rules precompute p50/p95/p99 and SLO error rates

### PII and data governance

- [ ] Attribute allowlist enforced by `RedactionSpanProcessor` on every exported span
- [ ] Log attributes filtered through the same allowlist before write
- [ ] `redact.test.ts` asserts no body/content/secret tokens appear in any exported
      attribute across a full turn
- [ ] `theo.telemetry.redactions_total` increments when a disallowed key is emitted

### Reliability of the telemetry pipeline

- [ ] Exporter queues are bounded (`maxQueueSize=2048`); overflow drops with
      `exporter_dropped_total`
- [ ] `exporter_backpressure.test.ts` proves the main loop is not blocked when the
      collector is unreachable
- [ ] SDK shutdown flushes queues with a 10s timeout; process exit does not block
      indefinitely

### SLOs, alerts, runbooks

- [ ] SLIs and SLOs defined in `src/telemetry/slos.ts` — compile-checked against
      `MetricsRegistry`
- [ ] Fast-burn and slow-burn alerts generated for each SLO via recording rules
- [ ] Binary alerts from the alerts table all configured with thresholds and `for:` windows
- [ ] Every alert rule has a `runbook_url` annotation; every runbook file exists and
      follows the four-heading structure (asserted by `alerts.test.ts`)
- [ ] Error-budget pre-merge gate blocks self-update when any SLO budget remaining < 10%;
      emits `SelfUpdateBlocked` event
- [ ] Silence/maintenance-window routing configured for planned self-update sessions

### Profiling and synthetic

- [ ] Pyroscope client captures CPU + heap profiles tagged with `version=<git sha>`
- [ ] Synthetic prober runs every 5 min; `probe_duration_ms` and `probe_failures_total`
      populated; `SyntheticProbeFailing` alert wired

### Meta-observability checks

- [ ] Prometheus scrapes itself, the collector (`:8888`), Loki, Tempo, Pyroscope
- [ ] `meta.json` dashboard visualises pipeline health
- [ ] Meta alerts (`CollectorRefusing`, `PromScrapeFailing`, `LokiIngestErrors`) configured

### Logging

- [ ] `TheoLogger` writes structured JSON to file + stdout
- [ ] Daily log rotation with 30-day gzip, 90-day deletion

### Tracing

- [ ] OTel tracer creates spans at four boundaries only: turn, retrieval, SDK, bus dispatch
- [ ] Traces export to OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; no-op otherwise
      (trace IDs still flow into logs)

### Self-update safety checks

- [ ] `runHealthCheck()` runs `just check` and tracks healthy_commit
- [ ] `rollbackToHealthy()` resets to healthy_commit and emits `system.rollback` event
- [ ] Engine startup: migrations → healthcheck → telemetry → bus → scheduler → gate
- [ ] `launchd` plist keeps Theo running through reboots and crashes
- [ ] `ops/install.sh` creates workspace dirs, installs plist, seeds healthy_commit,
      optionally starts observability stack

### Stack

- [ ] Local LGTM+P stack (Grafana, Loki, Tempo, Prometheus, Pyroscope, OTel Collector,
      Promtail) runs via `just observe-up` / `just observe-down`
- [ ] OTel Collector receives OTLP from Theo, tail-samples, fans out to Prometheus/Tempo
- [ ] Promtail tails `~/Theo/logs/` into Loki
- [ ] Grafana provisioning loads datasources, dashboards, alert rules, contact points,
      silences on startup
- [ ] All dashboards (`overview`, `cost`, `turns`, `goals`, `proposals`, `reflex`,
      `autonomy`, `bus`, `events`, `slos`, `profiling`, `meta`) parse and reference only
      instruments present in `MetricsRegistry`

### Events

- [ ] `system.rollback`, `system.degradation.healed`, `self_update.blocked`,
      `synthetic.probe.completed` event types added to SystemEvent union

### Pre-phase spike

- [ ] Bun + OTel JS SDK compatibility spike completed; any required polyfills or SDK
      alternatives documented; `just observe-up` proves end-to-end signal flow

- [ ] `just check` passes

## Test cases

### `tests/telemetry/logger.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| JSON format | Log at info level | Valid JSON with timestamp, level, message, component |
| Level filtering | Log at debug with level=info | Not written |
| File output | Log a message | Appears in log file |
| Trace correlation | Log within active span | `traceId` and `spanId` present |
| Rotation | Cross day boundary | New file created, previous file still readable |

### `tests/telemetry/metrics.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Registry completeness | Introspect `MetricsRegistry` keys | Matches cross-cutting metric table |
| Counter increment | Increment turns counter | Value increases |
| Histogram record | Record turn duration | Recorded in histogram |
| Observable gauge | Register node count callback | Callback invoked on collection |

### `tests/telemetry/projector.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Exhaustiveness | `handleEvent` over every `DomainEvent` variant | No TypeScript error; every case recorded |
| Turn completed | Emit `turn.completed` with 2-iteration usage | 2 input/output/cost samples with correct labels |
| Replay safety | Replay the same event twice through projector | Counters add twice (replay pushes through bus at-least-once; idempotency is bus-checkpoint-owned) |
| Autonomy violation | Emit `autonomy.violation` | Counter + warn log emitted |
| No side effects | Inject failing DB into deps | Projector never touches DB |

### `tests/telemetry/gauges.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Node count | Seed 10 nodes, trigger callback | Gauge observes 10 |
| Handler lag | Seed checkpoint 5 minutes old | Gauge observes ~300 for that handler |
| Pool usage | Hold 3 connections, trigger | Gauge observes 3 |

### `tests/telemetry/dashboards.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| JSON validity | Parse every dashboard under `ops/observability/grafana/dashboards/` | All parse |
| Metric references | Extract every `expr` and compare against `MetricsRegistry` key set | Every referenced metric exists |
| Recording rules referenced | Check SLO dashboard references `theo:slo:*` recording rules | All references resolve |

### `tests/telemetry/labels.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Closed set | Emit with value not in enum | `assertLabel` returns `unknown`, `cardinality_rejections_total` increments |
| Completeness | For every label used in projector, enum covers all values derivable from the `DomainEvent` union | No gaps |
| No free-form labels | Scan `metrics.ts`, `projector.ts`, `spans/*.ts` for `.add({...})` and `.record({...})` sites | Every label value is a reference to an enum in `labels.ts` |

### `tests/telemetry/redact.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Allowlist | Seed a full turn with spans carrying known-sensitive values (`"secret123"`, `"owner@example"`, message body) | No exported attribute contains any sensitive token |
| Redaction counter | Emit a disallowed key | `redactions_total{key}` increments with coarsened key |
| DB statement coarsening | Execute a parameterized query | `db.statement` shows `"SELECT FROM node WHERE ..."` — no bound parameters |
| Log filtering | Log with a disallowed attribute key | Written entry does not contain the value |

### `tests/telemetry/context.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Child across async | Start span A, emit event, dispatch to handler, handler calls `withSpan` | Handler's span is a child of A in the trace tree |
| Replay fidelity | Replay a historical event with `metadata.traceId` | Handler span re-uses the original `traceId` |
| No context | Event emitted outside any span | Handler span is a new root (not orphaned) |

### `tests/telemetry/semconv.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| DB spans | Every span in `spans/db.ts` | Uses `db.system`, `db.operation`, `db.statement` (not ad-hoc names) |
| Messaging spans | Bus dispatch spans | Use `messaging.system`, `messaging.destination.name`, `messaging.operation` |
| Lint pass | Scan `spans/*.ts` | No attribute name collides with an OTel convention under a different spelling |

### `tests/telemetry/resource.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Required attrs | Build resource | Contains `service.name`, `service.version`, `service.instance.id`, `deployment.environment` |
| Git sha version | `service.version` | Matches `git rev-parse HEAD` |
| Runtime attrs | Check | `process.runtime.name=bun`, `process.runtime.version` matches `Bun.version` |

### `tests/telemetry/sampling.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Error kept | Trace with a span marked `status=error` | Not sampled out |
| Slow kept | Trace with a span duration > 10s | Not sampled out |
| Success sampled | 100 success traces | ~20% retained (±5%) |

### `tests/telemetry/exporter_backpressure.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Blackhole exporter | Point OTLP at unreachable endpoint, run 100 turns | All 100 complete in bounded time; process does not hang |
| Queue overflow | Generate 10k spans in a burst | Spans beyond queue size dropped; `exporter_dropped_total` increments; saturation gauge peaks at 1.0 |
| Shutdown flush | Call `shutdown()` with backed-up queue | Returns within 10s; does not deadlock |

### `tests/telemetry/slos.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| SLI compiles | Every SLI references metrics in `MetricsRegistry` | No missing references |
| Burn-rate windows valid | Windows are `(long, short)` pairs per SRE workbook | All pairs valid |
| Budget exhaustion | Inject synthetic error rate; compute `error_budget_remaining_ratio` | Ratio decreases monotonically |

### `tests/telemetry/synthetic.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Probe succeeds | Chat engine returns ok | `probe_duration_ms` recorded; no failure |
| Probe times out | Chat engine stalls | `probe_failures_total{reason="timeout"}` increments |
| Probe event | Probe completes | `synthetic.probe.completed` event appears in log |

### `tests/telemetry/alerts.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Runbook url | Every alert in `rules.yaml` | Has `runbook_url` annotation |
| Runbook exists | URL resolves to file under `ops/observability/runbooks/` | File exists |
| Runbook structure | Parse each runbook | Contains headings: `What it means`, `Triage`, `Resolution`, `Related` |
| Expression parses | Each Prom expr and each Loki expr | Parses (via `promtool check rules` / `logcli`) |

### `tests/telemetry/recording_rules.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Parse | `promtool check rules recording_rules.yaml` | Exits 0 |
| Metric refs | Every expression references metrics in `MetricsRegistry` | All resolve |

### `tests/selfupdate/healthcheck.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Check passes | `just check` exits 0 | `{ ok: true }`, healthy_commit updated |
| Check fails | `just check` exits 1 | `{ ok: false }`, healthy_commit unchanged |
| No healthy commit | File missing | Error thrown (first run must seed it) |
| Idempotent | Run twice with no changes | Same result, no side effects |

### `tests/selfupdate/rollback.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Rollback executes | Healthcheck fails on startup | git reset to healthy_commit |
| Event emitted | Rollback occurs | `system.rollback` event in log (and projector increments `RollbackOccurred` alert basis) |
| No healthy commit | Rollback with missing file | Error, not silent failure |

## Risks

**Medium risk.**

1. **Bun + OTel JS SDK compatibility.** The OTel Node SDK assumes Node APIs (`async_hooks`,
   `perf_hooks`, process signals). Bun is largely compatible but has historical gaps.
   *Mitigation:* a pre-phase spike exercises the full pipeline (span creation, metric export,
   context propagation, shutdown) on Bun. Documented fallbacks: the `@opentelemetry/api`
   package (interfaces only) is always safe; if the SDK proves incompatible, a thin custom
   exporter built on `@opentelemetry/otlp-transformer` is acceptable.

2. **Observability sprawl creeps back in.** Without discipline, developers will reach for
   the logger or meter directly. *Mitigation:* biome `noRestrictedImports` rule, exhaustive
   projector, code-reviewer checklist item. Ad-hoc spans land in `src/telemetry/spans/`.

3. **Projector exhaustiveness churn.** Every new `DomainEvent` variant forces a projector
   update. *Mitigation:* this is the point — a single file to touch is better than a sweep.
   The compile error is the forcing function.

4. **Cardinality enum drift.** Adding a new `reason`/`gate`/`handler` value elsewhere
   without updating `labels.ts` silently lands in the `unknown` bucket. *Mitigation:*
   `cardinality_rejections_total` rate alert fires; `labels.test.ts` asserts completeness
   where it can be derived from types.

5. **PII leakage via new attributes.** A new attribute added to a span without a
   corresponding allowlist entry is redacted — but if someone also updates the allowlist
   carelessly, content could leak. *Mitigation:* allowlist changes require review;
   `redact.test.ts` runs fixture content through every span boundary.

6. **launchd configuration.** Plist syntax is fussy. Wrong paths or missing env vars cause
   silent failures. *Mitigation:* `install.sh` validates paths; a startup assertion verifies
   `THEO_WORKSPACE` exists and is writable.

7. **Self-update race.** A crash mid-update could leave the repo dirty. *Mitigation:*
   healthcheck + rollback; the error-budget pre-merge gate blocks risky updates.

8. **SLO noise during warmup.** After restart, burn-rate alerts over 5m windows spike
   because counters are low. *Mitigation:* every burn-rate alert has a `for: 2m` guard and
   a `min_samples` recording rule; warmup period suppressed via a routing silence.

9. **Collector / backend outage.** If OTel collector or Prometheus goes down, signals stop
   flowing. *Mitigation:* bounded queues with drop counters; `ExporterDropping` alert fires
   via the SDK's fallback log exporter; Prometheus `up{job="theo"}==0` triggers critical
   alert even if Theo itself is healthy.

10. **OTel dependency weight.** SDK + exporter packages add bulk. *Mitigation:* SDK loaded
    only when `OTEL_EXPORTER_OTLP_ENDPOINT` is configured.

11. **Local stack footprint.** Seven containers running 24/7. *Mitigation:* optional;
    Grafana Cloud is the documented zero-infra alternative.

12. **Dashboard drift.** *Mitigation:* `dashboards.test.ts` and `recording_rules.test.ts`
    fail CI if any reference resolves to an unknown metric.

13. **Log volume.** *Mitigation:* default `info`; 90-day retention with rotation and gzip
    caps disk use; Promtail drops debug older than 7d before shipping.

14. **Upcaster fidelity on the events panel.** *Mitigation:* documented tradeoff; read
    through TypeScript types when exact upcast shape matters. Typed custom views remain on
    the table as a future addition.

15. **Synthetic probe pollutes cost dashboards.** *Mitigation:* probe turns are tagged
    `gate=internal.scheduler` and filtered out of cost and latency SLOs; they have their
    own duration histogram.

16. **Profile storage cost.** Continuous profiling at 15s cadence can produce GB/day of
    profiles. *Mitigation:* Pyroscope retention 14d; sample rate tuneable per environment.
