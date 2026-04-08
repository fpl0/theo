# Phase 5: Feature Gating

## Motivation

Not all of Theo's capabilities should run in offline mode. Background intelligence (Phase 13)
uses a language model for contradiction detection, episode summarization, and pattern synthesis.
The scheduler (Phase 14) triggers autonomous agent turns. With a small local model, these
background tasks would produce low-quality results -- hallucinated contradictions, poor
summaries, unreliable autonomous actions. It is better to cleanly disable them and inform the
user than to run them badly.

This phase also adds MCP tool input validation as a safety net for malformed tool calls from
less capable models.

## Depends on

- **Phase 3** -- Engine knows the runtime mode
- **Foundation Phase 13** -- Background intelligence exists
- **Foundation Phase 12** -- Scheduler exists
- **Foundation Phase 9** -- MCP memory tools exist

## Scope

### Files to modify

| File | Change |
| ------ | -------- |
| `src/background/intelligence.ts` | Skip LLM-dependent jobs when mode is offline |
| `src/scheduler/runner.ts` | Gate autonomous turns on mode; allow user-triggered jobs |
| `src/mcp/tools.ts` | Add Zod validation at tool handler entry point |
| `src/events/types.ts` | Add `reason` field to `JobCancelledData` |
| `src/index.ts` | Pass runtime mode to background intelligence and scheduler |

## Design Decisions

### Background Intelligence: LLM Jobs Disabled Offline

Background intelligence tasks are model-dependent and quality-sensitive:

- **Contradiction detection** -- Requires comparing two knowledge nodes and classifying their
  relationship. A small model will produce false positives.
- **Episode summarization** -- Requires extracting salient information from transcripts. Quality
  drops significantly with smaller models.
- **Pattern synthesis** -- Requires identifying clusters of related nodes and synthesizing an
  abstraction. Too complex for a small model.
- **Importance propagation** -- Graph traversal only, no model call. Safe to run offline.
- **Forgetting curves** -- Time-based decay only, no model call. Safe to run offline.

The gating is at the job level, not the scheduler level:

```typescript
function shouldRunJob(
  jobType: BackgroundJobType,
  mode: "online" | "offline",
): boolean {
  if (mode === "online") return true;

  switch (jobType) {
    case "contradiction_detection":
    case "episode_summarization":
    case "pattern_synthesis":
    case "node_merging":
      return false;

    case "importance_propagation":
    case "forgetting_decay":
    case "access_count_update":
      return true;

    default: {
      const _exhaustive: never = jobType;
      throw new Error(`Unknown job type: ${_exhaustive}`);
    }
  }
}
```

### Scheduler: Gate Autonomous Turns

1. **Cron jobs** -- Time-based autonomous turns. **Disabled offline.**
2. **User-triggered jobs** -- Explicitly started by the user. **Allowed offline.**

```typescript
async function shouldExecuteJob(
  job: ScheduledJob,
  mode: "online" | "offline",
): Promise<boolean> {
  if (mode === "online") return true;
  return job.trigger === "user";
}
```

When a cron job is skipped, emit `job.cancelled` with reason:

```typescript
await bus.emit({
  type: "job.cancelled",
  version: 1,
  actor: "system",
  data: {
    jobId: job.id,
    reason: "offline_mode",
  },
  metadata: { jobId: job.id },
});
```

This requires adding `reason` to `JobCancelledData` in `src/events/types.ts`:

```typescript
interface JobCancelledData {
  readonly jobId: string;
  readonly reason: string; // NEW: "user_request" | "offline_mode" | etc.
}
```

### MCP Tool Input Validation

Small models produce malformed tool calls more often than Claude. The MCP memory tools define
JSON schemas for their inputs, but the SDK passes arguments through without validation. A
malformed `search_memory` call with `{ query: 42 }` instead of `{ query: "some text" }` would
reach the tool handler and cause a runtime error.

Add Zod validation at the tool handler entry point:

```typescript
// In each MCP tool handler:
const storeMemoryInput = z.object({
  body: z.string().min(1),
  kind: nodeKindSchema,
  confidence: z.number().min(0).max(1).optional(),
  // ... other fields
});

// Before executing:
const parsed = storeMemoryInput.safeParse(toolInput);
if (!parsed.success) {
  return {
    type: "tool_result",
    content: `Invalid arguments: ${parsed.error.issues.map((i) => i.message).join(", ")}`,
    is_error: true,
  };
}
```

This returns a structured error to the model, giving it a chance to retry with correct
arguments. The `maxTurns: 10` cap (Phase 3) prevents infinite retry loops.

This validation benefits online mode too (defense in depth), but is critical for offline mode
where malformed tool calls are common.

### Notification to User

When Theo starts in offline mode:

```text
[offline] Running with local model (qwen3.5:9b). Background intelligence and
autonomous scheduling are disabled. Memory retrieval and storage work normally.
```

### What Stays Enabled Offline

| Feature | Offline Status | Reason |
| ------- | -------------- | ------ |
| Conversation (chat) | Enabled | Core feature |
| Memory search (RRF) | Enabled | Database-only |
| Memory storage | Enabled | Database-only |
| Core memory read/update | Enabled | Database-only |
| Embeddings (local ONNX) | Enabled | Already local |
| User model read | Enabled | Database-only |
| User model update | Enabled (degraded) | Quality varies |
| Privacy filter | Enabled | Rule-based |
| Importance propagation | Enabled | Graph traversal only |
| Forgetting decay | Enabled | Time-based math only |
| Contradiction detection | **Disabled** | Requires strong classification |
| Episode summarization | **Disabled** | Requires strong summarization |
| Pattern synthesis | **Disabled** | Requires strong abstraction |
| Node merging | **Disabled** | Requires semantic comparison |
| Autonomous cron jobs | **Disabled** | Requires reliable reasoning |
| User-triggered jobs | Enabled | User is in the loop |

## Definition of Done

- [ ] `shouldRunJob()` skips LLM-dependent jobs in offline mode
- [ ] `importance_propagation` and `forgetting_decay` run in offline mode
- [ ] Scheduler skips autonomous cron jobs in offline mode
- [ ] Scheduler allows user-triggered jobs in offline mode
- [ ] Skipped cron jobs emit `job.cancelled` with `reason: "offline_mode"`
- [ ] `JobCancelledData` has a `reason` field in `src/events/types.ts`
- [ ] Every MCP tool handler validates input with Zod before executing
- [ ] Invalid tool input returns a structured error (not a crash)
- [ ] CLI displays offline mode notice at startup
- [ ] All gating uses exhaustive switch/case with `never` default
- [ ] `just check` passes

## Test Cases

### `tests/background/intelligence.test.ts` (additions)

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Online runs all | `mode: "online"`, all types | All return `true` |
| Offline skips LLM | `mode: "offline"`, LLM types | Returns `false` |
| Offline runs graph | `mode: "offline"`, non-LLM types | Returns `true` |

### `tests/scheduler/runner.test.ts` (additions)

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Online runs all | `mode: "online"`, cron job | Executes |
| Offline skips cron | `mode: "offline"`, cron job | Skipped, event emitted |
| Offline allows user | `mode: "offline"`, user-triggered | Executes |

### `tests/mcp/tools.test.ts` (additions)

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Valid input | Correct args | Tool executes normally |
| Invalid type | `query: 42` instead of string | Error result, no crash |
| Missing field | Required field absent | Error result with field name |
| Extra field | Unknown field in input | Stripped, tool executes |

## Risks

**Low risk.** Feature gating is straightforward boolean logic. MCP validation is standard Zod
usage. The main risk is being too aggressive -- disabling something that works fine with a small
model. The mitigation is that gating is per-job-type, so individual features can be re-enabled
by changing one line. Real-world testing will reveal which features are actually usable offline.
