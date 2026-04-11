# Phase 12a: Goal Loops & Executive Function

## Motivation

Phase 12 gives Theo a cron-driven heartbeat. Phase 14 gives Theo subagents — cognitive modes
for specialized work. Phase 12a composes them into **sustained agency**: Theo maintains a
persistent stack of active goals, picks the most valuable one, runs one turn of progress, and
yields. Across days and months, goals that are never touched fade via the forgetting curve.
Goals with concrete next actions get advanced, one turn at a time, without the owner having to
re-prompt.

The invariant that separates 12a from "a bigger scheduler": **every state change the executive
makes is an event**, and the runtime `goal_state` is a projection over those events. This makes
Theo's agency **fully resumable** (a crash mid-plan picks up exactly where it left off) and
**fully auditable** (the owner can replay the log for any `goal_id` and reconstruct every
decision).

The autonomy, trust propagation, replay semantics, priority scheduling, autonomy ladder, and
operator surface that 12a depends on are all defined in `docs/foundation.md` §7. This plan
instantiates them for goal execution.

## Depends on

- **Phase 3** — Event Log & Bus (event-sourced projections, transactional emit, handler
  checkpoints, decision/effect handler modes — the latter amended in Phase 13)
- **Phase 5** — Knowledge Graph (goals are stored as `NodeKind = 'goal'` nodes; tasks attach
  via typed edges)
- **Phase 8** — Privacy filter and trust tier (causation-based effective trust from
  `foundation.md §7.3`)
- **Phase 10** — Agent Runtime (SDK `query()` integration, context assembly)
- **Phase 11** — CLI Gate (operator commands `/goals`, `/pause`, `/cancel`, `/audit`)
- **Phase 12** — Scheduler (priority class integration with the tick loop)
- **Phase 13** — Background Intelligence (handler mode split: 12a's executive handler runs as
  an effect handler)
- **Phase 14** — Subagents (`planner`, `coder`, `researcher`, `writer` are dispatched from the
  executive loop; the circular dependency was resolved by reordering 12a to land **after**
  Phase 14)

## Scope

### Files to create

| File | Purpose |
| ---- | ------- |
| `src/db/migrations/0005_goal_loops.sql` | `goal_state`, `goal_task`, `goal_lease`, `resume_context`, `autonomy_policy` projections |
| `src/events/goals.ts` | `GoalEvent` union with full TypeScript payload interfaces |
| `src/goals/types.ts` | Branded `GoalId`, `TaskId`, `TurnId`, `RunnerId` + domain types |
| `src/goals/repository.ts` | `GoalRepository` — read the projection, write via bus.emit() |
| `src/goals/projection.ts` | Decision handler that maintains `goal_state` + `goal_task` from `goal.*` events |
| `src/goals/lease.ts` | `GoalLease` — per-goal advisory lock with heartbeat and expiry |
| `src/goals/executive.ts` | `ExecutiveLoop` — effect handler that runs one turn per tick |
| `src/goals/reconsideration.ts` | Intention reconsideration policy (§7.1) |
| `src/goals/stopping.ts` | Turn budget enforcement (maxTurns, maxBudgetUsd, maxDurationMs) |
| `src/goals/recovery.ts` | Startup recovery: emit `goal.task_abandoned` for dangling starts |
| `src/goals/mcp.ts` | `read_goals` MCP tool with trust-tier scoping |
| `src/goals/commands.ts` | Operator command handlers (`/goals`, `/pause`, etc.) |
| `tests/goals/projection.test.ts` | Every projection rule; replay rebuild matches live state |
| `tests/goals/lease.test.ts` | Acquire, heartbeat, expire, dangling recovery |
| `tests/goals/executive.test.ts` | Picks highest-priority, respects preemption, enforces budgets |
| `tests/goals/reconsideration.test.ts` | Single-minded commitment, reconsideration triggers |
| `tests/goals/recovery.test.ts` | Crash mid-task, restart, `goal.task_abandoned` synthesis |
| `tests/goals/poison.test.ts` | N consecutive failures → `goal.quarantined` |
| `tests/goals/fairness.test.ts` | Priority aging, no starvation |
| `tests/goals/mcp.test.ts` | `read_goals` trust filter + denylist |
| `tests/goals/commands.test.ts` | `/pause`, `/cancel`, `/audit` emit correct events |
| `tests/goals/security.test.ts` | External-origin goals cannot escalate; subagent tool allowlist |

### Files to modify

| File | Change |
| ---- | ------ |
| `src/events/types.ts` | Add `GoalEvent` group to top-level `Event` union |
| `src/memory/graph/types.ts` | Add `GoalNodeMetadata` shape for `NodeKind = 'goal'` metadata column |
| `src/memory/tools.ts` | Register `read_goals` tool in `memoryToolList()` |
| `src/scheduler/priority.ts` | (Phase 12 file) register executive loop as class = `executive` |

## Design Decisions

### 1. Goal representation — graph-native, not a parallel table

A goal is a **knowledge graph node** (`NodeKind = 'goal'`) with a structured `metadata` JSONB
column (Phase 13a adds the column). The body is the goal statement in natural language; the
metadata carries `{ title, description, origin, owner_priority }`. Goal nodes participate in
RRF retrieval, forgetting curves, importance propagation, and the abstraction hierarchy like
every other node. This eliminates the three-way naming collision described in `foundation.md
§7.1`: there is only one runtime "goal" object — the node — and `goal_state` is execution
state layered over it.

**Why not a parallel `goal` table?** Several reasons:

1. **RRF retrieval gets goals for free.** A user message mentioning "the Theo docs" retrieves
   both memory nodes *and* the active goal about writing the docs, in the same query. No
   separate "goal search" needed.
2. **Forgetting curves apply.** Goals that nobody touches fade exactly like other memories.
   The abstraction hierarchy can synthesize `pattern` nodes from multiple completed goals
   (e.g., "the owner prefers small scoped deliverables over monolithic pushes").
3. **Trust inheritance is uniform.** A goal created from an ideation proposal originating
   from a webhook-trust node inherits `external` trust and cannot escalate (§7.3).
4. **Partial orders via edges.** Sub-goals attach via `parent_goal` edges. Dependencies attach
   via `depends_on` edges. Conflicts surface via `contradicts` edges. The graph already
   supports all of this.
5. **Goal expiry is just decay to the floor.** `goal.expired` is a marker, not a destructive
   operation; the node stays findable by direct search.

The `goal_state` projection holds only the fast-changing *execution* state: `status`,
`current_task_id`, `consecutive_failures`, `leased_by`, `last_worked_at`, `quarantined_reason`.
This separation means the hot-path executive reads a narrow table and the memory system reads
the full node.

### 2. Migration — `0005_goal_loops.sql`

```sql
-- Execution state for active goals. Projected from goal.* events.
CREATE TABLE IF NOT EXISTS goal_state (
  node_id               integer     PRIMARY KEY REFERENCES node(id) ON DELETE CASCADE,
  status                text        NOT NULL DEFAULT 'proposed'
                        CHECK (status IN (
                          'proposed','active','blocked','paused','completed',
                          'cancelled','quarantined','expired'
                        )),
  origin                text        NOT NULL
                        CHECK (origin IN ('owner','ideation','reflex','system')),
  owner_priority        integer     NOT NULL DEFAULT 50
                        CHECK (owner_priority BETWEEN 0 AND 100),
  effective_trust       text        NOT NULL
                        CHECK (effective_trust IN (
                          'owner','owner_confirmed','verified',
                          'inferred','external','untrusted'
                        )),
  plan_version          integer     NOT NULL DEFAULT 0,
  plan                  jsonb       NOT NULL DEFAULT '[]',
  current_task_id       text,         -- ULID, NULL when no task in flight
  consecutive_failures  integer     NOT NULL DEFAULT 0,
  leased_by             text,         -- runner_id (ULID)
  leased_until          timestamptz,
  blocked_reason        text,
  quarantined_reason    text,
  last_reconsidered_at  timestamptz,
  last_worked_at        timestamptz,
  proposed_expires_at   timestamptz,  -- for ideation proposals
  redacted              boolean     NOT NULL DEFAULT false,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_goal_state_status_priority ON goal_state
  (status, owner_priority DESC, last_worked_at NULLS FIRST)
  WHERE status IN ('active','blocked');

CREATE INDEX IF NOT EXISTS idx_goal_state_lease ON goal_state (leased_until)
  WHERE leased_by IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_goal_state_proposed_expires ON goal_state
  (proposed_expires_at) WHERE status = 'proposed';

-- Per-task state. Plan step is keyed by task_id (ULID, assigned by goal.plan_updated).
CREATE TABLE IF NOT EXISTS goal_task (
  task_id         text        PRIMARY KEY,  -- ULID
  goal_node_id    integer     NOT NULL REFERENCES goal_state(node_id) ON DELETE CASCADE,
  plan_version    integer     NOT NULL,
  step_order      integer     NOT NULL,     -- position inside the plan snapshot
  body            text        NOT NULL,
  depends_on      text[]      NOT NULL DEFAULT '{}',  -- other task_ids
  status          text        NOT NULL DEFAULT 'pending'
                  CHECK (status IN (
                    'pending','in_progress','yielded','completed',
                    'failed','abandoned'
                  )),
  subagent        text,                          -- preferred subagent, if chosen
  started_at      timestamptz,
  completed_at    timestamptz,
  last_turn_id    text,                          -- ULID of most recent `goal.task_started`
  last_runner_id  text,                          -- ULID of executive process instance
  failure_count   integer     NOT NULL DEFAULT 0,
  yield_count     integer     NOT NULL DEFAULT 0,
  resume_key      text,                          -- points at resume_context.id
  redacted        boolean     NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_goal_task_goal ON goal_task (goal_node_id, plan_version, step_order);
CREATE INDEX IF NOT EXISTS idx_goal_task_status ON goal_task (status, goal_node_id)
  WHERE status IN ('in_progress','yielded','pending');

-- Opaque partial-turn state for preemption resume. Points-at, not replayable.
CREATE TABLE IF NOT EXISTS resume_context (
  id              text        PRIMARY KEY,  -- ULID
  goal_node_id    integer     NOT NULL REFERENCES goal_state(node_id) ON DELETE CASCADE,
  task_id         text        NOT NULL,
  turn_id         text        NOT NULL,
  session_id      text,
  snapshot        jsonb       NOT NULL,     -- opaque, whatever the subagent produced
  token_count     integer     NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz NOT NULL      -- 24 h from creation
);

CREATE INDEX IF NOT EXISTS idx_resume_context_goal ON resume_context (goal_node_id, task_id);
CREATE INDEX IF NOT EXISTS idx_resume_context_expires ON resume_context (expires_at);

-- Per-domain autonomy policy (foundation.md §7.7).
CREATE TABLE IF NOT EXISTS autonomy_policy (
  domain          text        PRIMARY KEY,
  level           integer     NOT NULL
                  CHECK (level BETWEEN 0 AND 5),
  set_by          text        NOT NULL,     -- actor kind
  set_at          timestamptz NOT NULL DEFAULT now(),
  reason          text
);

-- Seed default autonomy levels.
INSERT INTO autonomy_policy (domain, level, set_by) VALUES
  ('code.read',                    5, 'system'),
  ('code.write.workspace',         3, 'system'),
  ('code.write.theo_source',       0, 'system'),
  ('code.push.remote',             0, 'system'),
  ('messaging.draft',              3, 'system'),
  ('messaging.send',               0, 'system'),
  ('calendar.read',                5, 'system'),
  ('calendar.write',               4, 'system'),
  ('financial.read',               0, 'system'),
  ('financial.write',              0, 'system'),
  ('memory.write.ideation_origin', 2, 'system'),
  ('system.config',                0, 'system'),
  ('workspace.cleanup',            5, 'system')
ON CONFLICT (domain) DO NOTHING;

CREATE OR REPLACE TRIGGER trg_goal_state_updated_at
  BEFORE UPDATE ON goal_state
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

`node(kind='goal')` already exists from Phase 4 with the `metadata` column added in Phase 13a.
The `goal_state` table FKs to `node.id`, so goal creation is a two-step emit: `memory.node.created`
(with `kind='goal'`) then `goal.created` (with the newly-assigned node id). Both events run in a
single transaction via `bus.emit({ tx })`.

### 3. Event catalog — `src/events/goals.ts`

Every event type lists its handler mode (`decision` or `effect` per `foundation.md §7.4`) and
its payload shape. All events are version 1 (no upcasters during foundation).

```typescript
import type { EventId } from "./ids.ts";
import type { Actor } from "./types.ts";
import type { TrustTier } from "../memory/graph/types.ts";

// Branded IDs (also in src/goals/types.ts, re-exported here for event payload use)
export type GoalTaskId = string & { readonly __brand: "GoalTaskId" };
export type GoalTurnId = string & { readonly __brand: "GoalTurnId" };
export type GoalRunnerId = string & { readonly __brand: "GoalRunnerId" };

export type GoalOrigin = "owner" | "ideation" | "reflex" | "system";

export type GoalStatus =
  | "proposed" | "active" | "blocked" | "paused"
  | "completed" | "cancelled" | "quarantined" | "expired";

export type TaskStatus =
  | "pending" | "in_progress" | "yielded"
  | "completed" | "failed" | "abandoned";

export type BlockReason =
  | { readonly kind: "user_input"; readonly question: string }
  | { readonly kind: "resource"; readonly resource: string }
  | { readonly kind: "external"; readonly service: string }
  | { readonly kind: "budget"; readonly cap: "turn" | "goal" | "daily" }
  | { readonly kind: "degradation"; readonly level: number };

export type ReconsiderationReason =
  | "higher_priority_arrived"
  | "contradiction_detected"
  | "budget_exhausted"
  | "owner_command"
  | "periodic_review";

export type GoalTerminationReason =
  | "objective_met"
  | "no_longer_relevant"
  | "owner_cancelled"
  | "poison_quarantine"
  | "proposal_expired"
  | "superseded_by";

export interface PlanStep {
  readonly taskId: GoalTaskId;
  readonly body: string;
  readonly dependsOn: readonly GoalTaskId[];
  readonly preferredSubagent?: string | undefined;
}

export interface GoalCreatedData {
  readonly nodeId: number;                  // underlying NodeKind='goal' node
  readonly title: string;
  readonly description: string;
  readonly origin: GoalOrigin;
  readonly ownerPriority: number;           // 0..100, default 50
  readonly effectiveTrust: TrustTier;       // from causation chain, foundation.md §7.3
  readonly proposalExpiresAt?: string | undefined;  // ISO, only for proposed
}

export interface GoalConfirmedData {
  readonly nodeId: number;
  readonly confirmedBy: Actor;              // must be owner or owner_confirmed
}

export interface GoalPriorityChangedData {
  readonly nodeId: number;
  readonly oldPriority: number;
  readonly newPriority: number;
  readonly reason: string;
}

export interface GoalPlanUpdatedData {
  readonly nodeId: number;
  readonly planVersion: number;             // monotonically increasing
  readonly plan: readonly PlanStep[];       // FULL snapshot, never a diff
  readonly reason: string;
  readonly previousPlanHash: string | null; // enables drift detection
}

export interface GoalLeaseAcquiredData {
  readonly nodeId: number;
  readonly runnerId: GoalRunnerId;
  readonly leaseDurationMs: number;
}

export interface GoalLeaseReleasedData {
  readonly nodeId: number;
  readonly runnerId: GoalRunnerId;
  readonly reason: "normal" | "expiry" | "abandonment";
}

export interface GoalTaskStartedData {
  readonly nodeId: number;
  readonly taskId: GoalTaskId;
  readonly turnId: GoalTurnId;
  readonly runnerId: GoalRunnerId;
  readonly subagent: string;
  readonly maxTurns: number;
  readonly maxBudgetUsd: number;
  readonly maxDurationMs: number;
}

export interface GoalTaskProgressData {
  readonly nodeId: number;
  readonly taskId: GoalTaskId;
  readonly turnId: GoalTurnId;
  readonly note: string;                    // short progress summary
  readonly tokensConsumed: number;
  readonly costUsdConsumed: number;
}

export interface GoalTaskYieldedData {
  readonly nodeId: number;
  readonly taskId: GoalTaskId;
  readonly turnId: GoalTurnId;
  readonly resumeKey: string;               // resume_context.id
  readonly reason:
    | "preempted"
    | "turn_budget_exceeded"
    | "waiting_for_result";
}

export interface GoalTaskCompletedData {
  readonly nodeId: number;
  readonly taskId: GoalTaskId;
  readonly turnId: GoalTurnId;
  readonly outcome: string;                 // short summary for audit
  readonly artifactIds: readonly string[];  // ULIDs of memory nodes, PRs, etc.
  readonly totalTokens: number;
  readonly totalCostUsd: number;
}

export interface GoalTaskFailedData {
  readonly nodeId: number;
  readonly taskId: GoalTaskId;
  readonly turnId: GoalTurnId;
  readonly errorClass:
    | "tool_error" | "llm_error" | "validation_error"
    | "timeout" | "abort" | "internal";
  readonly message: string;
  readonly recoverable: boolean;
}

export interface GoalTaskAbandonedData {
  readonly nodeId: number;
  readonly taskId: GoalTaskId;
  readonly previousTurnId: GoalTurnId;
  readonly previousRunnerId: GoalRunnerId;
  readonly reason: "crash_recovery" | "lease_expired" | "force_abort";
}

export interface GoalBlockedData {
  readonly nodeId: number;
  readonly taskId: GoalTaskId;
  readonly blocker: BlockReason;
}

export interface GoalUnblockedData {
  readonly nodeId: number;
  readonly taskId: GoalTaskId;
  readonly unblockedBy: Actor;
}

export interface GoalReconsideredData {
  readonly nodeId: number;
  readonly reason: ReconsiderationReason;
  readonly outcome:
    | "stay_committed"
    | "plan_updated"
    | "goal_yielded"
    | "goal_abandoned";
}

export interface GoalPausedData {
  readonly nodeId: number;
  readonly pausedBy: Actor;
}

export interface GoalResumedData {
  readonly nodeId: number;
  readonly resumedBy: Actor;
}

export interface GoalCancelledData {
  readonly nodeId: number;
  readonly cancelledBy: Actor;
  readonly reason: GoalTerminationReason;
}

export interface GoalCompletedData {
  readonly nodeId: number;
  readonly finalOutcome: string;
  readonly totalTurns: number;
  readonly totalCostUsd: number;
}

export interface GoalQuarantinedData {
  readonly nodeId: number;
  readonly consecutiveFailures: number;
  readonly reason: string;
}

export interface GoalRedactedData {
  readonly nodeId: number;
  readonly redactedFields: readonly (
    "title" | "description" | "plan_bodies" | "task_bodies"
  )[];
  readonly redactedBy: Actor;
}

export interface GoalExpiredData {
  readonly nodeId: number;
}

export type GoalEvent =
  | TheoEvent<"goal.created",          GoalCreatedData>
  | TheoEvent<"goal.confirmed",        GoalConfirmedData>
  | TheoEvent<"goal.priority_changed", GoalPriorityChangedData>
  | TheoEvent<"goal.plan_updated",     GoalPlanUpdatedData>
  | TheoEvent<"goal.lease_acquired",   GoalLeaseAcquiredData>
  | TheoEvent<"goal.lease_released",   GoalLeaseReleasedData>
  | TheoEvent<"goal.task_started",     GoalTaskStartedData>
  | TheoEvent<"goal.task_progress",    GoalTaskProgressData>
  | TheoEvent<"goal.task_yielded",     GoalTaskYieldedData>
  | TheoEvent<"goal.task_completed",   GoalTaskCompletedData>
  | TheoEvent<"goal.task_failed",      GoalTaskFailedData>
  | TheoEvent<"goal.task_abandoned",   GoalTaskAbandonedData>
  | TheoEvent<"goal.blocked",          GoalBlockedData>
  | TheoEvent<"goal.unblocked",        GoalUnblockedData>
  | TheoEvent<"goal.reconsidered",     GoalReconsideredData>
  | TheoEvent<"goal.paused",           GoalPausedData>
  | TheoEvent<"goal.resumed",          GoalResumedData>
  | TheoEvent<"goal.cancelled",        GoalCancelledData>
  | TheoEvent<"goal.completed",        GoalCompletedData>
  | TheoEvent<"goal.quarantined",      GoalQuarantinedData>
  | TheoEvent<"goal.redacted",         GoalRedactedData>
  | TheoEvent<"goal.expired",          GoalExpiredData>;
```

Added to `src/events/types.ts`:

```typescript
import type { GoalEvent } from "./goals.ts";
export type Event = ChatEvent | MemoryEvent | SchedulerEvent | SystemEvent | GoalEvent;
```

### 4. Projection rules

Every column in `goal_state` and `goal_task` has exactly one event that writes it. The
projection handler is a **decision handler** — pure function of the event data, runs on both
live dispatch and replay. Non-determinism (LLM outputs) is captured in other events first,
then folded into the projection.

**`goal_state` projection rules.**

| Column | Event | Rule |
| ------ | ----- | ---- |
| `node_id` | `goal.created` | INSERT with node id |
| `status` | `goal.created` | `'proposed'` if `origin != 'owner'`, else `'active'` |
| `status` | `goal.confirmed` | `'proposed'` → `'active'` |
| `status` | `goal.blocked` | → `'blocked'` |
| `status` | `goal.unblocked` | → `'active'` |
| `status` | `goal.paused` | → `'paused'` |
| `status` | `goal.resumed` | → `'active'` or `'blocked'` (if still blocked) |
| `status` | `goal.cancelled` | → `'cancelled'` |
| `status` | `goal.completed` | → `'completed'` |
| `status` | `goal.quarantined` | → `'quarantined'` |
| `status` | `goal.expired` | → `'expired'` |
| `origin` | `goal.created` | set, immutable |
| `owner_priority` | `goal.created` | set from data |
| `owner_priority` | `goal.priority_changed` | set to `newPriority` |
| `effective_trust` | `goal.created` | set from data |
| `plan_version` | `goal.plan_updated` | set to `planVersion` |
| `plan` | `goal.plan_updated` | set to full `plan` snapshot |
| `current_task_id` | `goal.task_started` | set to `taskId` |
| `current_task_id` | `goal.task_completed` | cleared if equal to `taskId` |
| `current_task_id` | `goal.task_failed` | cleared if equal to `taskId` |
| `current_task_id` | `goal.task_yielded` | cleared if equal to `taskId` |
| `current_task_id` | `goal.task_abandoned` | cleared if equal to `taskId` |
| `current_task_id` | `goal.plan_updated` | cleared (new plan invalidates current task) |
| `consecutive_failures` | `goal.task_failed` | increment by 1 |
| `consecutive_failures` | `goal.task_completed` | reset to 0 |
| `leased_by`, `leased_until` | `goal.lease_acquired` | set |
| `leased_by`, `leased_until` | `goal.lease_released` | set to NULL |
| `blocked_reason` | `goal.blocked` | set to JSON string of `blocker` |
| `blocked_reason` | `goal.unblocked` | NULL |
| `quarantined_reason` | `goal.quarantined` | set from data |
| `last_reconsidered_at` | `goal.reconsidered` | set to `event.timestamp` |
| `last_worked_at` | `goal.task_started`\|`_progress`\|`_completed` | set to `event.timestamp` |
| `proposed_expires_at` | `goal.created` | set from data when present |
| `redacted` | `goal.redacted` | set true |

**`goal_task` projection rules.** `goal.plan_updated` inserts one row per `PlanStep` with
`status = 'pending'`, carrying `plan_version`. On a new `plan_updated`, previous-version rows
for the same goal are marked `abandoned` (via the projection handler, not a separate event).
`goal.task_started` sets `status = 'in_progress'`, `last_turn_id`, `last_runner_id`, `started_at`.
`goal.task_completed` sets `status = 'completed'`, `completed_at`, increments `resume_key`
cleanup. Identical rules for `failed`, `yielded`, `abandoned`. `failure_count` increments on
`goal.task_failed`; `yield_count` on `goal.task_yielded`.

**Idempotency.** Every projection write uses UPSERT keyed on the primary key, and the
handlers check that the incoming event's `turnId` matches or supersedes the stored
`last_turn_id` before mutating. This lets the projection tolerate duplicate event delivery
(at-least-once from the bus) without double-counting.

**Replay drift test.** A required test rebuilds `goal_state` and `goal_task` from zero by
replaying every `goal.*` event in the log and comparing the result, row by row, against the
live projection. Drift is a hard failure.

### 5. Lease / single-runner guarantee

Only one executive instance may advance a given goal at a time. The lease is a row in
`goal_state` with `leased_by = runner_id` and `leased_until = now() + lease_duration`. The
executive acquires a lease atomically via `SELECT FOR UPDATE SKIP LOCKED + UPDATE` inside a
transaction, emitting `goal.lease_acquired` in the same tx. Lease duration is short (default 5
minutes) with heartbeat renewal; if a runner dies mid-turn, the lease expires and another
runner can claim the goal. The expiring runner's `runner_id` is different from the claimant's,
so when the projection sees the next `goal.lease_acquired`, it also emits
`goal.task_abandoned` (via a decision handler on `lease_released` + `lease_acquired` pairs
that don't match up — see §7 recovery below).

```sql
-- Lease acquisition — atomic, fails on contention.
WITH eligible AS (
  SELECT node_id FROM goal_state
  WHERE status = 'active'
    AND redacted = false
    AND (leased_by IS NULL OR leased_until < now())
  ORDER BY
    owner_priority DESC,
    -- Priority aging: older last_worked_at promoted by 10 points per week of disuse.
    EXTRACT(EPOCH FROM (now() - COALESCE(last_worked_at, created_at))) / 604800.0 * 10 DESC,
    created_at ASC
  LIMIT 1
  FOR UPDATE SKIP LOCKED
)
UPDATE goal_state
SET leased_by = ${runnerId}, leased_until = now() + interval '5 minutes'
FROM eligible
WHERE goal_state.node_id = eligible.node_id
RETURNING goal_state.node_id;
```

`SKIP LOCKED` prevents contention from blocking. The row-level lock guarantees exactly-one
acquisition. The lease duration is renewed via `goal.lease_acquired` events with the same
`runnerId` — the projection treats same-runner renewals as idempotent.

### 6. Executive loop (effect handler)

The `ExecutiveLoop` runs as a **priority class `executive` task** in Phase 12's scheduler. On
each invocation it runs one turn:

```typescript
async function executeOneTurn(deps: ExecutiveDeps, ctx: ExecutionContext): Promise<void> {
  // 1. Acquire a lease on the highest-priority eligible goal.
  const goal = await deps.lease.acquire(ctx.runnerId);
  if (!goal) return;  // nothing to do

  try {
    // 2. Read current plan; if empty or stale, delegate to the planner subagent.
    const plan = await deps.goals.getPlan(goal.nodeId);
    if (plan.version < goal.minRequiredPlanVersion || plan.steps.length === 0) {
      await runPlanner(deps, goal, ctx);
      return;  // yield; next turn will start executing the plan
    }

    // 3. Pick the next ready task (dependencies satisfied, status pending).
    const task = await deps.goals.nextReadyTask(goal.nodeId);
    if (!task) {
      // Plan drained; mark goal completed if all tasks done, else wait.
      await maybeCompleteGoal(deps, goal);
      return;
    }

    // 4. Intention reconsideration check (foundation.md §7.1).
    const shouldReconsider = await deps.reconsideration.shouldReconsider(goal, task, ctx);
    if (shouldReconsider.reconsider) {
      await deps.bus.emit({
        type: "goal.reconsidered",
        version: 1,
        actor: "theo",
        data: {
          nodeId: goal.nodeId,
          reason: shouldReconsider.reason,
          outcome: shouldReconsider.outcome,
        },
        metadata: { causeId: ctx.causeEventId },
      });
      if (shouldReconsider.outcome !== "stay_committed") return;
    }

    // 5. Start the task turn.
    const turnId = newTurnId();
    await deps.bus.emit({
      type: "goal.task_started",
      version: 1,
      actor: "theo",
      data: {
        nodeId: goal.nodeId,
        taskId: task.id,
        turnId,
        runnerId: ctx.runnerId,
        subagent: task.preferredSubagent ?? "planner",
        maxTurns: ctx.budget.maxTurns,
        maxBudgetUsd: ctx.budget.maxBudgetUsd,
        maxDurationMs: ctx.budget.maxDurationMs,
      },
      metadata: { causeId: ctx.causeEventId, goalEffectiveTrust: goal.effectiveTrust },
    });

    // 6. Dispatch to subagent via SDK query(), respecting effective trust tier allowlist.
    const result = await dispatchSubagent(deps, goal, task, turnId, ctx);

    // 7. Emit terminal event based on result.
    switch (result.kind) {
      case "completed": return emitTaskCompleted(deps, goal, task, turnId, result);
      case "failed":    return emitTaskFailed(deps, goal, task, turnId, result);
      case "yielded":   return emitTaskYielded(deps, goal, task, turnId, result);
      case "blocked":   return emitGoalBlocked(deps, goal, task, result);
    }
  } finally {
    // 8. Always release the lease.
    await deps.lease.release(goal.nodeId, ctx.runnerId, "normal");
  }
}
```

**Handler mode.** `ExecutiveLoop.executeOneTurn` is registered as an **effect handler** on the
priority-class scheduler's `executive` slot. It does not run during bus replay. On startup, the
projection handlers rebuild `goal_state` / `goal_task` deterministically from existing events;
the executive loop starts fresh and picks up with new leases.

**Subagent dispatch and trust.** The subagent is invoked via `query()` with:

- `systemPrompt`: assembled from memory (`foundation.md §3.7`) plus a "current task" block,
  plus the advisor timing block (`foundation.md §4 Advisor-Assisted Execution`) when the
  subagent has `advisorModel` set.
- `allowedTools`: if `goal.effectiveTrust` is `external` or lower, restricted to the external
  turn allowlist from `foundation.md §7.6`. Otherwise the full subagent tool set.
- `settings.advisorModel`: set to the subagent's `advisorModel` (e.g. `claude-opus-4-6` for
  `planner`, `coder`, `researcher`, `writer`) when degradation level ≤ L1. Dropped above L1
  per the degradation table in `foundation.md §7.5`. The SDK subprocess attaches the
  `advisor-tool-2026-03-01` beta header and injects the server-side advisor tool.
- `maxTurns`, `maxBudgetUsd`: the goal's per-task budget, capped by the daily cloud-egress
  budget. Budget enforcement sums **both** executor and advisor iterations from
  `usage.iterations[]`.
- `abortController`: connected to the priority-class preemption signal.

Every event emitted by the subagent inherits the `goal.effectiveTrust` via the causation
chain, so downstream writes stay in tier.

**Advisor-aware cost accounting.** `goal.task_completed.data.totalCostUsd` must sum all
iterations from the SDK result, not read `total_cost_usd` directly:

```typescript
function extractTaskCost(result: SDKResultSuccess): { tokens: number; costUsd: number } {
  const iterations = result.usage.iterations ?? [];
  let tokens = 0;
  let costUsd = 0;
  for (const it of iterations) {
    const cached = it.cache_read_input_tokens ?? 0;
    const billableInput = it.input_tokens - cached;
    tokens += it.input_tokens + it.output_tokens;
    if (it.type === "advisor_message") {
      costUsd += advisorRate(it.model) * it.output_tokens;
      costUsd += advisorInputRate(it.model) * billableInput;
    } else {
      costUsd += executorOutputRate(result.model) * it.output_tokens;
      costUsd += executorInputRate(result.model) * billableInput;
    }
  }
  return { tokens, costUsd };
}
```

Rate tables live in `src/config.ts` and are updated when Anthropic publishes new pricing.

**Budgets.** Per-turn budget is enforced by `maxTurns`, `maxBudgetUsd`, and `maxDurationMs`.
Per-goal budget is enforced by the projection handler: before emitting `goal.task_started` the
executive checks cumulative cost across all `goal.task_*` events for this goal and refuses to
start new turns if the goal budget is exhausted. The refusal emits `goal.blocked` with
`blocker: { kind: "budget", cap: "goal" }`.

### 7. Recovery — dangling tasks at startup

If the process dies mid-turn, `goal.task_started` is in the log without a matching
`goal.task_completed | failed | yielded`. On startup, `src/goals/recovery.ts` walks the
projection and synthesizes `goal.task_abandoned` events for any `in_progress` task whose
`last_runner_id` doesn't match the current process `runnerId`. The abandoned events:

1. Clear `current_task_id` and `leased_by` in the projection.
2. Increment `failure_count` on the task.
3. Make the task `pending` again so the next executive tick picks it up.
4. Are subject to the poison-goal circuit breaker — repeated abandonment counts as failure.

This is the only safe way to resume: the original subagent's work is gone (it may have done
partial side effects, but those either emitted their own events or were transactional), and
re-running the task is cheaper than trying to reconstruct the partial state.

**Lease expiry without process death.** The same mechanism handles a live process that lost
network or DB connectivity long enough for its lease to expire. The next runner's first
action — seeing the stale lease and emitting `goal.lease_acquired` with a different
`runnerId` — triggers the decision handler that emits `goal.task_abandoned` for the prior
runner's in-flight task.

### 8. Poison goal circuit breaker

A goal that fails three turns in a row is **quarantined**. The projection handler watches
`consecutive_failures`:

```typescript
bus.on("goal.task_failed", async (event) => {
  const next = await deps.goals.readState(event.data.nodeId);
  if (next.consecutiveFailures >= POISON_THRESHOLD) {
    await deps.bus.emit({
      type: "goal.quarantined",
      version: 1,
      actor: "system",
      data: {
        nodeId: event.data.nodeId,
        consecutiveFailures: next.consecutiveFailures,
        reason: `${next.consecutiveFailures} consecutive failures in ` +
                `task ${event.data.taskId}: ${event.data.message}`,
      },
      metadata: { causeId: event.id },
    });
    // Notify owner — quarantine is always operator-visible.
    await deps.bus.emit({
      type: "notification.created",
      version: 1,
      actor: "system",
      data: {
        source: "goal-quarantine",
        body: `Goal "${event.data.nodeId}" quarantined after ${POISON_THRESHOLD} failures. ` +
              `/audit ${event.data.nodeId} for details.`,
      },
      metadata: { causeId: event.id },
    });
  }
}, { id: "poison-goal-circuit-breaker", mode: "decision" });
```

Quarantined goals are excluded from lease acquisition. The owner unquarantines via
`/resume <goal_id>` (which emits `goal.resumed` + resets `consecutive_failures` via a
`goal.priority_changed`-like reset event… see §10).

`POISON_THRESHOLD` is configurable (default 3). A single task's `failure_count` does not trip
quarantine — `consecutive_failures` tracks across tasks in the same goal, capturing "this
entire goal keeps falling over."

### 9. Priority aging and fairness

The lease acquisition query (§5) mixes `owner_priority DESC` with an aging term:
`time_since_last_worked / 1 week * 10`. A goal with `priority = 30` that hasn't been worked in
three weeks effectively becomes `priority = 60`, overtaking recent `priority = 50` goals. This
prevents starvation of low-priority goals and matches the single-minded commitment principle
(once committed, stay committed until done or yielded).

Aging is applied at selection time, not stored. `last_worked_at` is advanced on every task
event, so touching a goal resets its aging bonus. Aging constants are configurable via
`config.goals.agingWeeklyBonus`.

**Operator override.** `/pause <goal_id>` excludes a goal from selection entirely.
`/priority <goal_id> <level>` emits `goal.priority_changed`.

### 10. Owner commands → events

Every operator command emits a durable event. The CLI gate (phase 11) and Telegram gate (phase
TBD) translate `/pause gX` into `goal.paused { nodeId: X, pausedBy: "user" }`. The gate is
also the enforcement point for trust: commands over Telegram carry an implicit tier of
`verified` (authenticated but not CLI-origin), commands over CLI carry `owner`. The CLI gate is
the only path to autonomy-level changes, redaction, and consent.

Command handlers live in `src/goals/commands.ts` and are simple functions:

```typescript
async function pause(
  deps: CommandDeps,
  nodeId: number,
  actor: Actor,
): Promise<Result<void, CommandError>> {
  const state = await deps.goals.readState(nodeId);
  if (!state) return err({ code: "not_found" });
  if (state.status === "paused") return ok(undefined);  // idempotent

  await deps.bus.emit({
    type: "goal.paused",
    version: 1,
    actor,
    data: { nodeId, pausedBy: actor },
    metadata: {},
  });
  return ok(undefined);
}
```

Command handlers never read uncommitted state; they emit events and let the decision handler
reconcile.

### 11. `read_goals` MCP tool — trust-scoped

The agent reads active goals via the `read_goals` MCP tool, which is registered alongside the
memory tools from Phase 9. The tool filters by effective trust tier: a turn running at
`external` trust (from a webhook) only sees goals whose `effective_trust` is also `external`
or `untrusted` (equal or lower). This prevents webhook-triggered reflex turns from learning
about `owner_confirmed` goals.

```typescript
function readGoalsTool(deps: GoalDeps) {
  return tool(
    "read_goals",
    "Read active goals visible at your current trust tier. " +
      "Returns the goal list, their current tasks, and status. " +
      "Does not return goal bodies that are redacted.",
    {
      status: z.array(z.enum([
        "active", "blocked", "proposed", "paused", "quarantined",
      ])).optional(),
      includePlan: z.boolean().default(false),
    },
    async ({ status, includePlan }, extra) => {
      try {
        const effectiveTrust = extra.effectiveTrust as TrustTier;
        const goals = await deps.goals.listByTrust(effectiveTrust, { status, includePlan });
        return { content: [{ type: "text", text: formatGoalList(goals) }] };
      } catch (error) {
        return errorResult(error);
      }
    },
  );
}
```

`extra.effectiveTrust` is threaded through the SDK tool call metadata from Phase 10's chat
engine (Phase 10 scope amendment).

### 12. Notifications — body stripping

Goal state changes emit `notification.created` events. The notification data carries `source`
and `body`. The gate adapter layer (CLI, Telegram) applies body stripping rules from
`foundation.md §7.9`:

- CLI gate: full `body` rendered.
- Telegram gate: only the goal title and goal id; body is stripped to `[title only — use
  /goal <id> on CLI for details]`.

The stripping is implemented as a gate-side filter, not at the notification producer, so the
same `notification.created` event can be rendered differently per channel without a second
event type.

### 13. Integration with existing systems

**Phase 3 (Event Bus):** Phase 13 (Background Intelligence) amends the bus to support handler
modes. 12a's projection handlers are `decision`; its executive loop is `effect`. Required
before 12a can land.

**Phase 5 (Knowledge Graph):** Goals live as `NodeKind = 'goal'` nodes. Goal creation emits
`memory.node.created` first (transactionally, in the same `sql.begin` block as `goal.created`).

**Phase 7 (RRF):** Goal nodes participate in RRF. A user message mentioning "the docs" returns
both the relevant facts and the active goal about writing the docs. No special handling.

**Phase 8 (Privacy Filter):** The privacy filter's `checkPrivacy(content, effectiveTrust)` is
called by the goal command handlers before emitting `goal.created`, using the actor's
effective trust (computed from causation chain). A goal whose title/description contains
`restricted` content fails creation for `external`-tier actors.

**Phase 10 (Agent Runtime):** The chat engine threads `effectiveTrust` into the tool-call
metadata so `read_goals` and other tools can scope by it. The tool allowlist for external-tier
turns is set at the `query()` call site.

**Phase 12 (Scheduler):** The priority scheduler's `executive` class is served by
`ExecutiveLoop.executeOneTurn`. The scheduler's preemption `AbortController` is connected to
the subagent dispatch, so an incoming interactive message aborts mid-turn.

**Phase 13 (Background Intelligence):** Contradiction detection runs on `memory.node.created`.
When a contradiction involves a node that is referenced by an active goal plan, the
reconsideration module (`src/goals/reconsideration.ts`) is notified via a decision handler and
may emit `goal.reconsidered` on the next tick.

**Phase 14 (Subagents):** The `planner`, `coder`, `researcher`, `writer` subagents are the
dispatch targets for executive turns. The planner takes a goal body and current plan, returns
a new `PlanStep[]`, which becomes a `goal.plan_updated` event.

## Definition of Done

### Event sourcing correctness

- [ ] All 21 `goal.*` event types have concrete TypeScript payload interfaces
  in `src/events/goals.ts`
- [ ] `GoalEvent` union is added to the top-level `Event` union in `src/events/types.ts`
- [ ] Every goal event is version 1; no upcasters registered
- [ ] `PlanStep` is stable: rewriting it would require a new event type, not an upcaster
- [ ] `goal.plan_updated` carries full snapshot; unit test refuses a diff-shaped payload
- [ ] Replay rebuild: `goal_state` and `goal_task` identical after replay from zero
- [ ] Idempotency: replaying `goal.task_started` twice is a no-op; test emits the same
  event twice and asserts no double-state change
- [ ] Handler modes: projection handlers are `decision`, executive is `effect`; replay
  test skips effect handler

### Projection completeness

- [ ] Every column in `goal_state` has a projection rule driven by at least one event
  (no direct writes)
- [ ] Every column in `goal_task` has a projection rule
- [ ] `current_task_id` clears on `plan_updated`, `task_completed`, `task_failed`,
  `task_yielded`, `task_abandoned`
- [ ] `consecutive_failures` resets on `task_completed`, increments on `task_failed`
- [ ] `last_worked_at` is max timestamp across task events
- [ ] `status` transitions match the state machine (no `cancelled` → `active`,
  no `completed` → `proposed`, etc.)

### Executive correctness

- [ ] Executive acquires lease via `SKIP LOCKED + UPDATE` — concurrency test with
  two instances proves exactly-one
- [ ] Lease duration is honored; stale leases are re-acquirable after expiry
- [ ] Heartbeat renewal emits `goal.lease_acquired` with same `runnerId`, projection
  treats as idempotent
- [ ] Dangling tasks recovered on startup via `goal.task_abandoned`
- [ ] Force-abort (AbortController) emits `goal.task_abandoned` with `reason: "force_abort"`
- [ ] Per-turn budget enforced via `maxTurns`, `maxBudgetUsd`, `maxDurationMs`
- [ ] Per-goal budget enforced by summing task events before dispatch
- [ ] Cost accounting sums `usage.iterations[]` including `advisor_message` entries,
  not just `total_cost_usd`
- [ ] Subagent dispatch passes `settings.advisorModel` when subagent has `advisorModel`
  set and degradation ≤ L1
- [ ] Advisor timing block prepended to subagent system prompt when advisor is enabled
- [ ] Degradation level L2 strips advisor from all executive turns
- [ ] Poison goal (3 consecutive failures) → `goal.quarantined` + `notification.created`
- [ ] Priority aging promotes stale goals; unit test with old-but-low-priority and
  new-but-medium-priority asserts correct order
- [ ] Executive yields to interactive class within 2 s drain window

### Intention reconsideration

- [ ] Single-minded commitment by default — no reconsideration mid-task unless triggered
- [ ] Reconsideration triggers: higher priority class arrives, contradiction detected,
  budget exhausted, owner command, periodic (every N turns)
- [ ] Reconsideration emits `goal.reconsidered` with structured reason
- [ ] Outcome `plan_updated` triggers planner subagent and emits new `goal.plan_updated`
- [ ] Outcome `goal_yielded` releases lease and goes back to scheduler

### Security / trust

- [ ] `effective_trust` on goal inherited from causation chain
- [ ] `read_goals` scoped to effective trust tier; test with external-trust turn
  asserts higher-tier goals invisible
- [ ] Subagent dispatch in external-trust turn uses `EXTERNAL_TURN_TOOLS` allowlist
- [ ] Goal creation runs through `checkPrivacy(content, effectiveTrust)`
- [ ] Ideation-origin goals hard-capped at autonomy level 2 regardless of domain
- [ ] Denylist violations emit `autonomy.denylist_violation` and hard-fail
- [ ] Subagent dispatch scrubs secret env vars (`ANTHROPIC_API_KEY`,
  `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`) before spawning

### Operator surface

- [ ] `read_goals` MCP tool returns goals filtered by trust tier, status, and redaction flag
- [ ] `/goals` command returns active goals via CLI and Telegram
- [ ] `/goal <id>` returns goal details; body stripped on Telegram
- [ ] `/pause`, `/resume`, `/cancel`, `/audit`, `/promote`, `/priority` emit the right events
- [ ] `/autonomy` is CLI-only and writes `autonomy_policy`
- [ ] `/redact` is CLI-only, emits `goal.redacted`, projection masks body
- [ ] `notification.created` with `source = "goal-quarantine"` is delivered
- [ ] Telegram gate body-strips notifications by default

### Testing

- [ ] `just check` passes
- [ ] Every test case below passes
- [ ] Regression: all prior phase tests still pass

## Test Cases

### `tests/goals/projection.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Create owner goal | `origin = "owner"` | `status = "active"` |
| Create ideation proposal | `origin = "ideation"` | `status = "proposed"` |
| Confirm proposal | `goal.confirmed` after proposed | `status = "active"` |
| Priority change | `goal.priority_changed` | `owner_priority` updated |
| Plan update | `goal.plan_updated` v1 | `plan_version = 1`, `plan` set, tasks inserted |
| Plan update overwrites | `goal.plan_updated` v2 | Previous tasks marked abandoned, new tasks inserted, `current_task_id` cleared |
| Task started | `goal.task_started` | `current_task_id` set, `last_worked_at` updated |
| Task completed | `goal.task_completed` | `current_task_id` cleared, `consecutive_failures` reset |
| Task failed | `goal.task_failed` | `consecutive_failures` incremented |
| Task yielded | `goal.task_yielded` | `current_task_id` cleared, `yield_count` incremented |
| Block + unblock | `goal.blocked` → `goal.unblocked` | `blocked_reason` set then cleared; `status` toggles |
| Pause + resume | `goal.paused` → `goal.resumed` | `status` transitions |
| Cancel terminal | `goal.cancelled` | `status = "cancelled"`, no further events change state |
| Redaction | `goal.redacted` | `redacted = true`, body masked in repository reads |
| Expire | `goal.expired` on proposed | `status = "expired"` |
| Replay rebuild | Replay all events from empty DB | Row-by-row match against live projection |
| Out-of-order replay | `plan_updated` before `created` | UPSERT handles; final state correct after full replay |
| Double emit idempotent | Same `goal.task_started` twice | Projection unchanged on second |

### `tests/goals/lease.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Acquire free | No lease held | Lease acquired, `leased_by` set |
| Acquire contested | Another runner holds lease | Returns null, no emit |
| Acquire expired | Lease expired | New runner acquires, emits `goal.lease_acquired` |
| Heartbeat renewal | Same runner emits again | `leased_until` extended |
| Release normal | `goal.lease_released` | `leased_by` cleared |
| Concurrency: two runners race | 10 parallel acquires | Exactly one succeeds |

### `tests/goals/executive.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Pick highest priority | Two active goals, different priorities | Higher picked |
| Aging promotes stale | Old priority=30 vs new priority=50 | Old picked after 2 weeks of staleness |
| Skip paused | Paused goal available | Not picked |
| Skip quarantined | Quarantined goal | Not picked |
| Full plan execution | 3-task plan | Tasks advance in order, `goal.completed` emitted at end |
| Dependency blocks | Task B depends on incomplete A | B not picked |
| Turn budget exceeded | `maxTurns` hit | `goal.task_yielded` with `reason: "turn_budget_exceeded"` |
| Wall-clock budget | `maxDurationMs` hit | AbortController fires, `goal.task_yielded` |
| Per-goal budget exceeded | Cumulative cost > cap | `goal.blocked` with `blocker.kind = "budget", cap = "goal"` |
| Preemption drain | Interactive arrives mid-turn | Yielded within 2 s, `goal.task_yielded` with `reason: "preempted"` |
| Force abort after drain | Handler doesn't yield in 2 s | `goal.task_abandoned` with `reason: "force_abort"` |

### `tests/goals/reconsideration.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Default commitment | Mid-task, no triggers | `shouldReconsider = false` |
| Higher priority arrives | Interactive event in queue | `reason = "higher_priority_arrived"` |
| Contradiction detected | Contradiction on referenced node | `reason = "contradiction_detected"` |
| Budget exhausted | Per-goal budget near cap | `reason = "budget_exhausted"` |
| Owner /pause command | Pause event processed | `reason = "owner_command"` |
| Periodic review trigger | 10 turns since last reconsideration | `reason = "periodic_review"` |
| Stay committed outcome | Reconsider but plan is sound | `outcome = "stay_committed"` |
| Plan update outcome | Reconsider and plan is stale | `outcome = "plan_updated"` |

### `tests/goals/recovery.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Dangling task at startup | `task_started` without terminator | `goal.task_abandoned` synthesized with `reason: "crash_recovery"` |
| Multiple dangling | Three tasks in flight when process died | All three abandoned |
| Stale lease | Lease held by dead runner | New runner acquires, abandons previous task |
| Clean restart | No dangling tasks | No abandonment events |
| Abandonment counts as failure | Abandoned task → failure_count++ | Consecutive abandonments trigger quarantine |

### `tests/goals/poison.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Single failure | `goal.task_failed` | No quarantine |
| Three consecutive failures | Three `task_failed` events | `goal.quarantined` emitted |
| Failure then success resets | fail, complete, fail | Counter reset by completion |
| Notification on quarantine | Quarantine emitted | `notification.created` with source `"goal-quarantine"` |
| Quarantined goal not picked | Lease acquisition query | Skips quarantined |

### `tests/goals/fairness.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| Aging weeks boost | 2-week stale priority 30 | Picked over fresh priority 40 |
| Fresh doesn't starve stale | Repeated selection over month | Stale goal worked within N ticks |
| Manual priority override | `/priority gX 80` | Immediate selection next tick |

### `tests/goals/mcp.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| read_goals at owner tier | All statuses | Returns all goals |
| read_goals at external tier | Has owner and external goals | Returns only external/untrusted |
| read_goals on redacted | Redacted goal | Body masked, title visible if not redacted |
| Status filter | `status: ["active"]` | Only active returned |
| Include plan | `includePlan: true` | Plan steps included |

### `tests/goals/commands.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| /pause emits event | Valid goal | `goal.paused` emitted |
| /pause idempotent | Already paused | No new event |
| /cancel emits | Valid goal | `goal.cancelled` emitted |
| /audit returns history | Goal with events | Chronological list |
| /promote proposed | Proposed goal | `goal.confirmed` emitted |
| /priority change | Set to 70 | `goal.priority_changed` emitted |
| /redact CLI only | Telegram attempts | Denied |

### `tests/goals/security.test.ts`

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| External-trust turn tool allowlist | Reflex dispatch | Tools limited to read-only |
| Trust inheritance | Subagent writes via causation chain | New memory capped at turn's effective trust |
| Ideation-origin autonomy cap | Goal with `origin = "ideation"` | Cannot exceed autonomy level 2 |
| Denylist violation | Subagent attempts to write `.env.local` | `autonomy.denylist_violation` emitted, action denied |
| Env var scrub | Subagent spawn | `ANTHROPIC_API_KEY` absent from subagent env |

## Risks

**High risk** — the executive loop is the first component in Theo that runs side-effecting
handlers autonomously and modifies event-sourced state. Every gap in projection rules, lease
correctness, or preemption handling compounds into silent drift or double-execution. The
mitigations below map to the failure modes the review surfaced.

### Data integrity

1. **Projection drift.** If a handler bug causes `goal_state` to diverge from the event log,
   the executive sees a stale view and may double-execute tasks or skip valid ones.
   *Mitigation:* required replay-rebuild test (`tests/goals/projection.test.ts`) reconstructs
   the projection from zero and compares byte-for-byte with live state. Run as part of
   `just check`. Any drift is a CI blocker.
2. **Lease acquisition race.** Two parallel runners could acquire the same goal if the
   `SKIP LOCKED` discipline is wrong. *Mitigation:* required concurrency test with 10 parallel
   acquire attempts.
3. **Duplicate event emission.** A retrying handler could emit `goal.task_started` twice.
   *Mitigation:* projection UPSERTs by `(task_id, turn_id)` — second write is a no-op. Test
   covers this.

### Availability

1. **Poison goal blocks queue.** A goal that fails forever starves all others. *Mitigation:*
   circuit breaker at 3 consecutive failures quarantines the goal and emits a notification.
2. **Starvation of low-priority goals.** Always picking the highest-priority means stale
   low-priority goals never run. *Mitigation:* priority aging (10 points per week of
   staleness). Test covers.
3. **Lease held by dead process.** Kills the goal until the lease expires. *Mitigation:* short
   default lease (5 min) + heartbeat renewal; startup recovery emits `task_abandoned` for the
   dead runner's tasks.
4. **Deadlock on user input.** Two goals both wait for user response; user replies to one,
   leaving the other stuck. *Mitigation:* `goal.blocked` carries structured `blocker.kind =
   "user_input"`; `message.received` events can target-unblock via the gate's command
   handlers.

### Security

1. **Trust laundering across causation hops.** Addressed by `foundation.md §7.3`.
   *Mitigation:* effective trust stored on every event, walked from `causeId`, enforced at
   repository boundary. Tests cover.
2. **Prompt injection via goal content.** A `goal.created` body could contain attack text
   that ends up in the executive's prompt. *Mitigation:* external-origin goals (ideation from
   untrusted nodes, reflex from webhooks) run with the external turn tool allowlist from
   `foundation.md §7.6`, which cannot execute arbitrary instructions.
3. **Owner confusion over ideation vs owner origin.** An ideation-proposed goal could be
   indistinguishable from an owner goal after confirmation. *Mitigation:* `origin` column is
   append-only; `goal.confirmed` does not change it; `/goals` renders origin explicitly.

### Cost

1. **Runaway executive cost.** An active goal keeps failing → wastes subagent budget.
   *Mitigation:* per-turn budget, per-goal budget, daily budget, circuit breaker. Four
   independent caps.
2. **Budget bypass via plan_updated loop.** Planner revises plan instead of completing, costs
   compound. *Mitigation:* `plan_version` counter; reconsideration cost counts against the
   per-goal budget; hard cap on `plan_version` (default 10 per goal).

### Operational

1. **Opaque failure modes.** A failed goal with `tool_error` message `"undefined is not a
   function"` is not actionable. *Mitigation:* structured `errorClass` field, `/audit` command
   renders the full causation chain, poison quarantine always notifies.
2. **Redaction incomplete.** Redacting a goal does not delete the event log; backups still
   contain the body. *Mitigation:* plan documents this explicitly; `/redact` surfaces a
   notice that archival copies must be re-encrypted.

## Out of scope (future phases)

- **Multi-user goal assignment.** Theo is single-owner; multi-tenant goals are not in scope.
- **Cross-device execution.** The lease is per-process; future multi-node Theo would require
  distributed lease coordination.
- **Goal scheduling by deadline.** Owner can set priority but not "run by Thursday 5pm". A
  future phase could add deadline events and scheduler integration.
- **Automated autonomy promotion.** The self model calibrates, but raising autonomy levels is
  always an explicit owner command. No automatic climbing of the ladder.
