# Phase 12a: Goal Loops & Executive Function

## Motivation

To move Theo from a "pulsed" chatbot to a "constant partner," he needs an executive
function. Instead of just running scheduled maintenance, Theo should maintain a
persistent stack of long-term goals and actively work on them in the background.

**Event-Driven Agency:** Critically, the Planner does not just write to a table. It
emits events to the **Event Log**. The "current plan" is a projection of these events.
This ensures that Theo's agency is fully resumable (he can crash and pick up exactly
where he left off) and auditable (you can "replay" the log to see exactly why he
pivoted from one task to another).

## Depends on

- **Phase 12** — Scheduler (base execution logic)
- **Phase 14** — Subagents (Planner, Coder, Researcher)

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/db/migrations/0005_goals.sql` | Goal projections and execution history |
| `src/events/goals.ts` | Goal event types (`goal.created`, `goal.plan_updated`, `goal.task_completed`, etc.) |
| `src/memory/goals.ts` | `GoalRepository` — managing goal projections from the event log |
| `src/scheduler/executive.ts` | `ExecutiveLoop` — the goal-driven heartbeat emitting events |
| `tests/memory/goals.test.ts` | Goal lifecycle, priority, and task state tests |
| `tests/scheduler/executive.test.ts` | Executive loop behavior, goal switching, and termination |

## Design Decisions

### Goal Events (The Source of Truth)

Every change the Planner makes is recorded as a durable event:

- `goal.created`: A new high-level objective is defined.
- `goal.plan_updated`: The Planner breaks the goal into sub-tasks or adjusts the plan.
- `goal.task_started`: Theo begins work on a specific sub-task.
- `goal.task_completed`: Theo finishes a task and records the outcome.
- `goal.blocked`: Work is paused because user input or an external resource is required.

### Goal Projections

The `goal` table is a projection of the events above. Replaying the log for a specific
`goal_id` reconstructs the entire history of how that goal evolved.

```sql
CREATE TABLE IF NOT EXISTS goal (
  id              text        PRIMARY KEY, -- ULID
  title           text        NOT NULL,
  description     text        NOT NULL,
  priority        integer     NOT NULL DEFAULT 50,
  status          text        NOT NULL DEFAULT 'active',
  plan            jsonb       NOT NULL DEFAULT '[]', -- The "Current Plan" projection
  current_task_idx integer    NOT NULL DEFAULT 0,
  last_worked_at  timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
```

### The Executive Loop

The `ExecutiveLoop` acts as the driver. It reads the current **projection**, identifies
the next task, and invokes the appropriate subagent. The subagent's output is then
emitted as a `goal.task_completed` event, which in turn updates the projection.

## Definition of Done

- [ ] `goal.*` event types added to the global Event union
- [ ] `GoalRepository` correctly projects goal state from the event log
- [ ] `ExecutiveLoop` picks the highest priority active goal and runs one "turn" of progress
- [ ] Every state change in the loop (start, complete, block) emits a durable event
- [ ] The current plan is visible to the agent via an MCP tool `read_goals`
- [ ] Theo reports goal progress and blocks via `notification.created` events
- [ ] `just check` passes

## Test Cases

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Event Replay | Delete goal table and replay log | Goal state is perfectly reconstructed |
| Resumability | Stop process mid-task | On restart, Theo identifies the same task as "in progress" |
| Audit Trail | Query events for a specific Goal ID | Chronological log of every plan change and task result |
