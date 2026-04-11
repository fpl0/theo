# Phase 15: Operationalization

## Cross-cutting dependencies

Observability must cover the autonomous agency layer introduced by Phases 12a and 13b.
The metric list below extends Phase 15's baseline:

| Metric | Type | Phase | Tracks |
| ------ | ---- | ----- | ------ |
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
| `theo.advisor.iterations_total` | Counter | 14 | Advisor sub-inferences (by subagent) |
| `theo.advisor.cost_usd_total` | Counter | 14 | Cumulative advisor cost (by model) |
| `theo.autonomy.violations_total` | Counter | 13b | Denylist violations by domain |

Additionally, Phase 15 owns the **degradation healing timer** that emits
`degradation.level_changed` events when conditions improve for a configurable window (see
`foundation.md §7.5`). The healing timer runs as a periodic task inside the engine's
scheduler tick loop.

The observability layer reads `usage.iterations[]` from every turn result and emits one
metric sample per iteration type (`message` vs `advisor_message`) so cost dashboards can
break down executor vs advisor contribution.

## Motivation

Theo runs on macOS as an always-on process with complete OS access, access to its own source code
(GitHub), and a dedicated workspace. It must survive reboots, crashes, and self-updates. Without
proper operationalization, Theo is a development prototype that dies on restart and provides no
visibility into what it's doing.

This phase makes Theo production-ready for a single-machine deployment: always running via
`launchd`, self-updating with rollback safety, and fully observable through OpenTelemetry-compatible
structured logging, tracing, and metrics.

## Depends on

- **Phase 14** — Engine lifecycle (startup/shutdown sequence)
- **Phase 11** — CLI gate (for log inspection commands)
- **Phase 12** — Scheduler (background jobs need observability)

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/telemetry/logger.ts` | Structured JSON logger with file + stdout output, daily rotation |
| `src/telemetry/tracer.ts` | OTel-compatible tracing: spans for turns, tool calls, retrieval |
| `src/telemetry/metrics.ts` | Counters and gauges: turns, tokens, cost, memory growth, latency |
| `src/telemetry/index.ts` | Unified telemetry setup, OTel collector configuration |
| `src/selfupdate/healthcheck.ts` | `just check` runner, healthy_commit tracking |
| `src/selfupdate/rollback.ts` | Git rollback to healthy_commit on startup failure |
| `ops/com.theo.agent.plist` | `launchd` plist for always-on macOS deployment |
| `ops/install.sh` | One-shot setup: install plist, create workspace dirs, configure env |
| `tests/telemetry/logger.test.ts` | Log formatting, rotation, level filtering |
| `tests/telemetry/metrics.test.ts` | Counter/gauge registration, export format |
| `tests/selfupdate/healthcheck.test.ts` | Check pass/fail, commit tracking |
| `tests/selfupdate/rollback.test.ts` | Rollback execution, event emission |

### Files to modify

| File | Change |
| ------ | -------- |
| `src/engine.ts` | Integrate telemetry init on startup, healthcheck before bus start |
| `src/chat/engine.ts` | Add trace spans around turn lifecycle |
| `src/memory/retrieval.ts` | Add trace spans + latency metrics around RRF queries |

## Design Decisions

### Workspace Layout

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

### Structured Logging

Every log entry is a JSON line:

```typescript
interface LogEntry {
  readonly timestamp: string;     // ISO 8601
  readonly level: "debug" | "info" | "warn" | "error";
  readonly message: string;
  readonly traceId?: string;
  readonly spanId?: string;
  readonly component: string;     // "engine", "bus", "retrieval", "scheduler", etc.
  readonly attributes: Record<string, unknown>;
}
```

The logger writes to two destinations simultaneously:

1. **File** — daily-rotated files in `~/Theo/logs/`. Files older than 30 days are gzipped. Files
   older than 90 days are deleted.
2. **stdout** — for `launchd` to capture and for development.

```typescript
class TheoLogger {
  private readonly logDir: string;
  private currentFile: BunFile | null = null;
  private currentDate: string | null = null;

  constructor(private readonly config: { workspace: string; level: LogLevel }) {
    this.logDir = `${config.workspace}/logs`;
  }

  async log(level: LogLevel, message: string, attributes?: Record<string, unknown>): Promise<void> {
    if (LEVEL_ORDER[level] < LEVEL_ORDER[this.config.level]) return;

    const entry: LogEntry = {
      timestamp: new Date().toISOString(),
      level,
      message,
      component: attributes?.component as string ?? "theo",
      traceId: getActiveTraceId(),
      spanId: getActiveSpanId(),
      attributes: attributes ?? {},
    };

    const line = JSON.stringify(entry) + "\n";
    console.log(line.trimEnd());
    await this.writeToFile(line);
  }
}
```

### OTel-Compatible Tracing

Each user message creates a root span. Child spans cover the full turn lifecycle:

```text
turn (root span)
├── context.assemble
│   ├── core_memory.read
│   ├── user_model.read
│   ├── skills.find_by_trigger
│   └── retrieval.rrf_search
├── sdk.query
│   ├── tool.store_memory
│   ├── tool.search_memory
│   └── tool.update_core
├── hooks.stop
└── bus.handlers
    ├── auto_edges
    └── contradiction_check
```

Tracing uses the `@opentelemetry/api` package for span creation and context propagation. The actual
exporter is configurable:

- **OTLP exporter** — when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, traces export to a collector
  (Jaeger, Grafana Tempo, etc.)
- **Log exporter** — when no collector is configured, trace summaries (span name, duration, status)
  are written as structured log entries. Degraded but still useful for debugging.

```typescript
import { trace, SpanStatusCode } from "@opentelemetry/api";

const tracer = trace.getTracer("theo");

async function handleMessage(body: string, gate: string): Promise<TurnResult> {
  return tracer.startActiveSpan("turn", async (span) => {
    try {
      span.setAttribute("gate", gate);
      span.setAttribute("message.length", body.length);

      const systemPrompt = await tracer.startActiveSpan("context.assemble", async (child) => {
        const result = await assembleSystemPrompt(deps, body);
        child.end();
        return result;
      });

      // ... SDK query, hooks, etc.

      span.setStatus({ code: SpanStatusCode.OK });
      return { ok: true, response: responseBody };
    } catch (error) {
      span.setStatus({ code: SpanStatusCode.ERROR, message: String(error) });
      throw error;
    } finally {
      span.end();
    }
  });
}
```

### Metrics

Key metrics as OTel instruments:

```typescript
import { metrics } from "@opentelemetry/api";

const meter = metrics.getMeter("theo");

const turnCounter = meter.createCounter("theo.turns.total");
const turnDuration = meter.createHistogram("theo.turns.duration_ms");
const inputTokens = meter.createCounter("theo.tokens.input");
const outputTokens = meter.createCounter("theo.tokens.output");
const costCounter = meter.createCounter("theo.cost.usd");
const nodeGauge = meter.createObservableGauge("theo.memory.nodes");
const retrievalLatency = meter.createHistogram("theo.retrieval.duration_ms");
const cacheHitRate = meter.createObservableGauge("theo.cache.hit_rate");
```

Observable gauges are populated by callbacks that query the database periodically (every 60s).
Counters are incremented inline in the relevant code paths.

When no OTel collector is configured, a periodic log exporter dumps key metrics to the structured
log file every hour.

### Always Running via launchd

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
  </dict>
  <key>ThrottleInterval</key><integer>10</integer>
</dict>
</plist>
```

`ThrottleInterval: 10` prevents rapid restart loops if the process crashes immediately. The Engine's
startup sequence (migrations → bus replay → scheduler → gate) handles recovery on restart.

### Self-Update Safety

```typescript
interface HealthCheckResult {
  readonly ok: boolean;
  readonly commit: string;        // current HEAD
  readonly healthyCommit: string; // last known-good
  readonly errors?: string[];
}

async function runHealthCheck(workspace: string): Promise<HealthCheckResult> {
  const commit = await Bun.$`git rev-parse HEAD`.text();
  const healthyCommit = await readHealthyCommit(workspace);

  // Run the quality gate
  const check = await Bun.$`just check`.quiet().nothrow();

  if (check.exitCode === 0) {
    // Current commit is healthy — update the marker
    await writeHealthyCommit(workspace, commit.trim());
    return { ok: true, commit: commit.trim(), healthyCommit: commit.trim() };
  }

  return {
    ok: false,
    commit: commit.trim(),
    healthyCommit,
    errors: [check.stderr.toString()],
  };
}

async function rollbackToHealthy(workspace: string, bus: EventBus): Promise<void> {
  const healthy = await readHealthyCommit(workspace);
  if (!healthy) throw new Error("No healthy commit recorded — cannot rollback");

  await Bun.$`git reset --hard ${healthy}`;

  await bus.emit({
    type: "system.rollback",
    version: 1,
    actor: "system",
    data: {
      fromCommit: await Bun.$`git rev-parse HEAD`.text(),
      toCommit: healthy,
      reason: "healthcheck_failed",
    },
    metadata: {},
  });
}
```

**Startup sequence with healthcheck:**

```text
Engine.start()
  → run migrations
  → run healthcheck
     → if FAIL: rollback to healthy_commit, restart
     → if PASS: update healthy_commit
  → start bus (replay from checkpoints)
  → start scheduler
  → start gate
```

**Branch discipline for self-updates:**

- Theo commits to feature branches, never directly to main
- Opens PRs via `gh pr create`
- Auto-merges when `just check` passes on the branch
- `healthy_commit` always points to a main commit that passed checks

### Install Script

```bash
#!/bin/bash
# ops/install.sh — one-shot setup for Theo on macOS

WORKSPACE="${THEO_WORKSPACE:-$HOME/Theo}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Create workspace
mkdir -p "$WORKSPACE/logs" "$WORKSPACE/data" "$WORKSPACE/config"

# Record initial healthy commit
git -C "$REPO_DIR" rev-parse HEAD > "$WORKSPACE/data/healthy_commit"

# Install launchd plist
PLIST="$HOME/Library/LaunchAgents/com.theo.agent.plist"
sed "s|/Users/owner|$HOME|g; s|/Users/owner/Code/theo|$REPO_DIR|g" \
  "$REPO_DIR/ops/com.theo.agent.plist" > "$PLIST"

# Load the agent
launchctl load "$PLIST"

echo "Theo installed. Workspace: $WORKSPACE"
echo "Logs: $WORKSPACE/logs/"
echo "Stop: launchctl unload $PLIST"
```

## Definition of Done

- [ ] `TheoLogger` writes structured JSON to file + stdout
- [ ] Daily log rotation with 30-day gzip, 90-day deletion
- [ ] OTel tracer creates spans for turn lifecycle, context assembly, retrieval, tool calls
- [ ] Traces export to OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set
- [ ] Traces degrade to structured log entries when no collector is configured
- [ ] Key metrics registered: turns, tokens, cost, memory size, retrieval latency
- [ ] Metrics export to OTLP or log-based fallback
- [ ] `runHealthCheck()` runs `just check` and tracks healthy_commit
- [ ] `rollbackToHealthy()` resets to healthy_commit and emits `system.rollback` event
- [ ] Engine startup integrates healthcheck before bus replay
- [ ] `launchd` plist keeps Theo running through reboots and crashes
- [ ] `ops/install.sh` creates workspace dirs, installs plist, seeds healthy_commit
- [ ] `system.rollback` event type added to SystemEvent union
- [ ] `just check` passes

## Test Cases

### `tests/telemetry/logger.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| JSON format | Log at info level | Valid JSON with timestamp, level, message, component |
| Level filtering | Log at debug with level=info | Not written |
| File output | Log a message | Appears in log file |
| Trace correlation | Log within active span | traceId and spanId present |

### `tests/telemetry/metrics.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Counter increment | Increment turns counter | Value increases |
| Histogram record | Record turn duration | Recorded in histogram |
| Observable gauge | Register node count callback | Callback invoked on collection |

### `tests/selfupdate/healthcheck.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Check passes | `just check` exits 0 | `{ ok: true }`, healthy_commit updated |
| Check fails | `just check` exits 1 | `{ ok: false }`, healthy_commit unchanged |
| No healthy commit | File missing | Error thrown (first run must seed it) |
| Idempotent | Run twice with no changes | Same result, no side effects |

### `tests/selfupdate/rollback.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Rollback executes | Healthcheck fails on startup | git reset to healthy_commit |
| Event emitted | Rollback occurs | `system.rollback` event in log |
| No healthy commit | Rollback with missing file | Error, not silent failure |

## Risks

**Medium risk.**

1. **launchd configuration** — plist syntax is fussy. Wrong paths or missing env vars cause silent
   failures. The install script must validate paths before writing the plist.

2. **Self-update race condition** — If Theo updates its own code while running, the running process
   uses the old code until restart. The update protocol (commit → check → merge → restart) handles
   this, but a crash mid-update could leave the repo in a dirty state. The healthcheck + rollback
   mechanism is the safety net.

3. **OTel dependency weight** — The `@opentelemetry/api` package is lightweight (just interfaces),
   but the SDK packages (exporters, span processors) add bulk. Use the API package for
   instrumentation, make the SDK/exporter packages optional — loaded only when
   `OTEL_EXPORTER_OTLP_ENDPOINT` is configured.

4. **Log volume** — An active Theo generating 100+ turns/day with debug logging could produce large
   log files. Default to `info` level, with `debug` available via config override. The 90-day
   retention policy caps total disk usage.

**Mitigations:**

- Test the plist locally before documenting it as the canonical approach
- The rollback mechanism is the ultimate safety net for self-update failures
- OTel instrumentation uses the lightweight API package; heavy SDK is optional
- Log rotation and retention are built into the logger, not left to external tools
