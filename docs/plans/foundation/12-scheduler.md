# Phase 12: Scheduler

## Cross-cutting dependencies

The original draft of this phase used a simple `maxConcurrent: 1` cap. The reviewed 12a
and 13b plans require a richer scheduler: **four priority classes with preemption via
`AbortController`**. This phase is the authoritative home for that scheduler; 12a and
13b register their classes with it.

From `docs/foundation.md §7.5`:

1. **Four priority classes**: `interactive` > `reflex` > `executive` > `ideation`.
2. **Single runner** at a time. Only one class's task actively runs.
3. **Preemption**. Higher-class arrivals abort the currently running lower-class turn
   via the shared `AbortController`. The preempted handler has 2 s to emit its
   `*.yielded` event and drain state before force-abort.
4. **Bounded queues** per class (default: interactive = 4, reflex = 16, executive = 8,
   ideation = 2) with class-specific overflow behavior (coalesce, defer, drop).
5. **Degradation ladder** (L0 healthy → L4 down). Level changes are
   `degradation.level_changed` events. Level is a projection. Each level restricts which
   classes and whether the advisor is allowed to run. See `foundation.md §7.5` table.
6. **Cost accounting** reads `usage.iterations[]` for every turn — executor iterations
   are billed at the executor rate, `type: "advisor_message"` iterations at the advisor
   rate. `max_budget_usd` is enforced by summing both.
7. **Resume context** table for preempted turns — opaque state per turn that lets the
   next executor pick up mid-plan. Referenced by `goal.task_yielded.resumeKey` (12a).

This phase creates the priority scheduler; 12a registers the `executive` class and 13b
registers the `reflex` and `ideation` classes. Phase 15 adds the degradation level healing
timer.

## Motivation

The scheduler is what makes Theo an agent, not a chatbot. It acts without being asked — running
consolidation to keep memory clean, reflecting on behavioral patterns, scanning for forgotten
commitments, and making autonomous progress on goals.

A chatbot waits for input. An agent initiates. The scheduler gives Theo a heartbeat of autonomous
activity, running on cron schedules or one-off triggers. Each job is a full SDK `query()` turn with
access to memory tools, isolated context, and event recording.

## Depends on

- **Phase 3** — Event bus (job events)
- **Phase 10** — Chat engine (jobs run via SDK `query()`)

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/db/migrations/0004_scheduler.sql` | Scheduled jobs + execution tracking tables |
| `src/scheduler/types.ts` | `ScheduledJob`, `JobExecution`, `JobId` branded type |
| `src/scheduler/store.ts` | `JobStore` — CRUD for jobs and executions |
| `src/scheduler/cron.ts` | Cron expression parsing via `cron-parser` |
| `src/scheduler/runner.ts` | `Scheduler` — tick loop, execution, overdue handling |
| `src/scheduler/builtin.ts` | Built-in job definitions (consolidation, reflection, scan, goals) |
| `tests/scheduler/store.test.ts` | Job CRUD |
| `tests/scheduler/cron.test.ts` | Cron parsing, next-run computation |
| `tests/scheduler/runner.test.ts` | Tick execution, overdue detection, concurrency |

## Design Decisions

### Migration: `0004_scheduler.sql`

```sql
CREATE TABLE IF NOT EXISTS scheduled_job (
  id              text        PRIMARY KEY,  -- ULID
  name            text        NOT NULL UNIQUE,
  cron            text,                      -- null for one-off
  agent           text        NOT NULL DEFAULT 'main',
  prompt          text        NOT NULL,
  enabled         boolean     NOT NULL DEFAULT true,
  max_duration_ms integer     NOT NULL DEFAULT 300000,  -- 5 min default
  max_budget_usd  numeric(6,4) NOT NULL DEFAULT 0.10,
  last_run_at     timestamptz,
  next_run_at     timestamptz NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE TRIGGER trg_scheduled_job_updated_at
  BEFORE UPDATE ON scheduled_job
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TABLE IF NOT EXISTS job_execution (
  id              text        PRIMARY KEY,  -- ULID
  job_id          text        NOT NULL REFERENCES scheduled_job(id) ON DELETE CASCADE,
  status          text        NOT NULL DEFAULT 'running',  -- running, completed, failed
  started_at      timestamptz NOT NULL DEFAULT now(),
  completed_at    timestamptz,
  duration_ms     integer,
  tokens_used     integer,
  cost_usd        numeric(8,4),
  error_message   text,
  result_summary  text,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_job_execution_job ON job_execution (job_id);
CREATE INDEX IF NOT EXISTS idx_scheduled_job_next_run ON scheduled_job (next_run_at)
  WHERE enabled = true;
```

### Job ID

Jobs use a dedicated `JobId` branded type, separate from `EventId`:

```typescript
type JobId = string & { readonly __brand: "JobId" };
function newJobId(): JobId { return ulid() as JobId; }

type ExecutionId = string & { readonly __brand: "ExecutionId" };
function newExecutionId(): ExecutionId { return ulid() as ExecutionId; }
```

### Job Types

```typescript
interface ScheduledJob {
  readonly id: JobId;
  readonly name: string;
  readonly cron: string | null;   // null for one-off
  readonly agent: string;         // subagent name
  readonly prompt: string;
  readonly enabled: boolean;
  readonly maxDurationMs: number;
  readonly maxBudgetUsd: number;
  readonly lastRunAt: Date | null;
  readonly nextRunAt: Date;
  readonly createdAt: Date;
}

interface JobExecution {
  readonly id: ExecutionId;
  readonly jobId: JobId;
  readonly status: "running" | "completed" | "failed";
  readonly startedAt: Date;
  readonly completedAt: Date | null;
  readonly durationMs: number | null;
  readonly tokensUsed: number | null;
  readonly costUsd: number | null;
  readonly errorMessage: string | null;
  readonly resultSummary: string | null;
}

interface SubagentDefinition {
  readonly model: string;
  readonly maxTurns: number;
  readonly systemPromptPrefix: string;
}

interface SchedulerConfig {
  readonly tickIntervalMs: number;  // default 60_000
  readonly maxConcurrent: number;   // default 1
}
```

### Cron Parser

Use the `cron-parser` npm package (MIT license, well-maintained, standard 5-field cron):

```typescript
import CronParser from "cron-parser";

function nextRun(expression: string, from: Date): Date {
  const interval = CronParser.parseExpression(expression, { currentDate: from });
  return interval.next().toDate();
}

function matches(expression: string, date: Date): boolean {
  const prev = nextRun(expression, new Date(date.getTime() - 60_000));
  // Match if the next run from 1 minute before lands on this minute
  return prev.getMinutes() === date.getMinutes()
    && prev.getHours() === date.getHours()
    && prev.getDate() === date.getDate();
}
```

Install with `bun add cron-parser`.

### Scheduler Runner

```typescript
class Scheduler {
  private intervalId: Timer | null = null;
  private running = false;
  private readonly activeJobs = new Set<JobId>();

  constructor(
    private readonly store: JobStore,
    private readonly bus: EventBus,
    private readonly memoryServer: McpSdkServerConfigWithInstance,
    private readonly subagents: Record<string, SubagentDefinition>,
    private readonly config: SchedulerConfig,
  ) {}

  async start(): Promise<void> {
    // Seed built-in jobs if they don't exist
    for (const job of BUILTIN_JOBS) {
      const existing = await this.store.getByName(job.name);
      if (!existing) {
        await this.store.create({
          ...job,
          id: newJobId(),
          nextRunAt: nextRun(job.cron!, new Date()),
        });
      }
    }

    // Run overdue jobs first
    await this.runOverdueJobs();

    // Start tick loop
    this.intervalId = setInterval(() => void this.tick(), this.config.tickIntervalMs);
    this.running = true;
  }

  async stop(): Promise<void> {
    if (this.intervalId) clearInterval(this.intervalId);
    this.running = false;
    // Wait for any in-flight jobs to complete
    while (this.activeJobs.size > 0) {
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }

  private async tick(): Promise<void> {
    const now = new Date();
    const dueJobs = await this.store.getDueJobs(now);

    for (const job of dueJobs) {
      if (this.activeJobs.has(job.id)) continue;
      if (this.activeJobs.size >= this.config.maxConcurrent) break;
      await this.executeJob(job);
    }
  }

  private async executeJob(job: ScheduledJob): Promise<void> {
    const executionId = newExecutionId();
    this.activeJobs.add(job.id);

    await this.bus.emit({
      type: "job.triggered",
      version: 1,
      actor: "scheduler",
      data: { jobId: job.id, executionId, jobName: job.name },
      metadata: {},
    });

    const execution = await this.store.createExecution(job.id, executionId);

    try {
      const result = await this.runAgentTurn(job);

      await this.store.completeExecution(executionId, {
        status: "completed",
        durationMs: Date.now() - execution.startedAt.getTime(),
        resultSummary: result.summary,
        tokensUsed: result.tokensUsed,
        costUsd: result.costUsd,
      });

      await this.bus.emit({
        type: "job.completed",
        version: 1,
        actor: "scheduler",
        data: { jobId: job.id, executionId, jobName: job.name, summary: result.summary },
        metadata: {},
      });

      // Surface findings as notifications
      if (result.notification) {
        await this.bus.emit({
          type: "notification.created",
          version: 1,
          actor: "scheduler",
          data: { source: job.name, body: result.notification },
          metadata: {},
        });
      }
    } catch (error) {
      await this.store.completeExecution(executionId, {
        status: "failed",
        durationMs: Date.now() - execution.startedAt.getTime(),
        errorMessage: error instanceof Error ? error.message : String(error),
      });
      await this.bus.emit({
        type: "job.failed",
        version: 1,
        actor: "scheduler",
        data: {
          jobId: job.id,
          executionId,
          jobName: job.name,
          error: error instanceof Error ? error.message : String(error),
        },
        metadata: {},
      });
    } finally {
      this.activeJobs.delete(job.id);
    }

    // Compute next run
    if (job.cron) {
      const nextRunAt = nextRun(job.cron, new Date());
      await this.store.updateNextRun(job.id, nextRunAt);
    } else {
      // One-off: disable after execution
      await this.store.disable(job.id);
    }
  }

  private async runAgentTurn(job: ScheduledJob): Promise<JobResult> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), job.maxDurationMs);

    try {
      const subagent = this.subagents[job.agent];
      if (!subagent) throw new Error(`Unknown subagent: ${job.agent}`);

      const jobSystemPrompt = [
        subagent.systemPromptPrefix,
        `You are executing scheduled job "${job.name}".`,
        "Use memory tools to read and write relevant information.",
        "Be concise in your output — summarize findings and actions taken.",
      ].join("\n");

      const result = query({
        prompt: job.prompt,
        options: {
          model: subagent.model,
          mcpServers: { memory: this.memoryServer },
          systemPrompt: jobSystemPrompt,
          settingSources: [],
          allowedTools: ["mcp__memory__*"],
          maxTurns: subagent.maxTurns,
          maxBudgetUsd: job.maxBudgetUsd,
          persistSession: false,
          permissionMode: "bypassPermissions",
          abortController: controller,
        },
      });

      let responseText = "";
      let tokensUsed: number | undefined;
      let costUsd: number | undefined;

      for await (const message of result) {
        if (message.type === "result" && message.subtype === "success") {
          responseText = message.result;
          tokensUsed = message.usage?.output_tokens;
          costUsd = message.total_cost_usd;
        }
      }

      return {
        summary: responseText.slice(0, 500),
        notification: responseText,
        tokensUsed,
        costUsd,
      };
    } finally {
      clearTimeout(timeout);
    }
  }

  private async runOverdueJobs(): Promise<void> {
    const overdueJobs = await this.store.getOverdueJobs(new Date());
    // Run each once (not once per missed tick)
    for (const job of overdueJobs) {
      await this.executeJob(job);
    }
  }
}

interface JobResult {
  readonly summary: string;
  readonly notification: string;
  readonly tokensUsed: number | undefined;
  readonly costUsd: number | undefined;
}
```

### Built-in Jobs

```typescript
const BUILTIN_JOBS: readonly ScheduledJobInput[] = [
  {
    name: "consolidation",
    cron: "0 */6 * * *",    // every 6 hours
    agent: "consolidator",
    prompt: "Review recent episodes and knowledge graph. " +
      "Compress old episodes into summaries, " +
      "deduplicate similar nodes, capture projection " +
      "snapshots.",
    maxDurationMs: 600_000,  // 10 min
    maxBudgetUsd: 0.15,
  },
  {
    name: "reflection",
    cron: "0 3 * * 0",      // weekly, Sunday 3am
    agent: "reflector",
    prompt: "Analyze behavioral patterns from the " +
      "past week. Update self-model calibration. " +
      "Note any recurring themes or shifts.",
    maxDurationMs: 300_000,  // 5 min
    maxBudgetUsd: 0.10,
  },
  {
    name: "proactive-scan",
    cron: "0 9 * * *",      // daily 9am
    agent: "scanner",
    prompt: "Surface any forgotten commitments, " +
      "pending follow-ups, or upcoming deadlines " +
      "from memory. Create notifications for " +
      "anything time-sensitive.",
    maxDurationMs: 180_000,  // 3 min
    maxBudgetUsd: 0.08,
  },
  {
    name: "goal-execution",
    cron: "0 10 * * 1-5",   // weekdays 10am
    agent: "main",
    prompt: "Review active goals. Make autonomous " +
      "progress on any goal that has a clear next " +
      "step. Record what was done.",
    maxDurationMs: 600_000,  // 10 min
    maxBudgetUsd: 0.15,
  },
];
```

Built-in jobs are seeded in `Scheduler.start()` — each job is inserted only if no job with that name
already exists.

### MCP Tools for Scheduling

Added to the memory server (or a separate scheduler server):

- `schedule_job` — Create a new job (cron or one-off)
- `list_jobs` — List active jobs
- `cancel_job` — Disable a job

## Definition of Done

- [ ] `bun add cron-parser` installed
- [ ] `just migrate` applies scheduler migration
- [ ] `JobId` and `ExecutionId` are branded types (not reusing `EventId`)
- [ ] `JobStore` creates, reads, updates, and disables jobs (including `getByName`)
- [ ] `cron-parser` correctly computes next run for standard 5-field expressions
- [ ] Scheduler constructor takes `memoryServer` and `subagents` as dependencies
- [ ] `Scheduler.start()` seeds built-in jobs if they don't exist
- [ ] Scheduler tick loop picks up due jobs and executes them
- [ ] `runAgentTurn()` uses SDK `query()` with `AbortController` for timeout enforcement
- [ ] Job execution emits `job.triggered`, `job.completed`/`job.failed` events
- [ ] Failed jobs record error and emit failure event
- [ ] One-off jobs disable after execution
- [ ] Overdue jobs run once on startup (not once per missed tick)
- [ ] Notifications emitted for findings worth reporting
- [ ] `schedule_job`, `list_jobs`, `cancel_job` MCP tools work
- [ ] `just check` passes

## Test Cases

### `tests/scheduler/cron.test.ts`

| Test | Expression | From | Expected Next |
| ------ | ----------- | ------ | -------------- |
| Every minute | `* * * * *` | 10:30:15 | 10:31:00 |
| Every hour | `0 * * * *` | 10:30:00 | 11:00:00 |
| Every 6 hours | `0 */6 * * *` | 10:00:00 | 12:00:00 |
| Weekdays 9am | `0 9 * * 1-5` | Friday 10:00 | Monday 9:00 |
| Weekly Sunday | `0 3 * * 0` | Monday | Next Sunday 3:00 |

### `tests/scheduler/store.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Create job | Valid input | Job stored with `JobId` branded type |
| Get by name | Existing name | Returns job |
| Get by name | Unknown name | Returns null |
| Get due jobs | Jobs with next_run_at < now | Returns due jobs |
| Get overdue | next_run_at in the past | Returns overdue jobs |
| Disable job | Cancel one-off | enabled = false |
| Create execution | Running job | Execution with status=running |
| Complete execution | Finish job | Status, duration, summary, cost recorded |

### `tests/scheduler/runner.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Start seeds built-in jobs | First startup | Built-in jobs created in store |
| Start skips existing jobs | Job with same name exists | Not duplicated |
| Tick executes due job | Job due now | Job executed, events emitted |
| Tick skips running job | Job already executing | Not started again |
| Tick skips disabled | Job disabled | Not executed |
| Tick respects maxConcurrent | Two due, maxConcurrent=1 | Only one runs |
| Overdue on start | Job 2 hours overdue | Run once (not twice) |
| One-off cleanup | One-off executed | Job disabled after |
| Failed job | Execution throws | Status=failed, error recorded |
| Timeout enforcement | Job exceeds maxDurationMs | AbortController fires, job fails |
| Next run computed | After successful cron job | next_run_at advanced |
| Notification emitted | Job has findings | `notification.created` event |
| RunAgentTurn uses correct subagent | Job with agent="consolidator" | Subagent model and maxTurns used |

## Risks

**Medium risk.** The `cron-parser` package is mature and well-tested, removing cron parsing as a
risk. The main risk is the interaction between scheduled jobs and user conversations — they share
the event bus and memory, so concurrent writes must be safe. PostgreSQL handles this via
transactions, but the application code must not hold in-memory state that could become stale.

The job execution via SDK `query()` means each job spawns a subprocess, which is resource-heavy. The
default `maxConcurrent: 1` prevents resource exhaustion but means jobs queue if they overlap. The
`AbortController` + `setTimeout` pattern ensures runaway jobs are terminated after `maxDurationMs`.
