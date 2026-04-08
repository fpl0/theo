# Phase 3: Engine Adaptation

## Motivation

The `ChatEngine` currently calls `query()` with hardcoded Anthropic assumptions: Claude model
names, thinking mode, USD budget, Anthropic token accounting. This phase makes the engine
mode-aware so the same `handleMessage()` code path works for both online (Claude) and offline
(Ollama + local model) without branching into separate engines.

This is the highest-risk phase. It modifies the core agent loop and introduces process-level env
var management. Every decision here is designed to fail safe.

## Depends on

- **Phase 2** -- Config extension with `ResolvedRuntime`
- **Foundation Phase 10** -- `ChatEngine` exists with `handleMessage()`, hooks, streaming

## Scope

### Files to modify

| File | Change |
| ------ | -------- |
| `src/chat/engine.ts` | Accept `ResolvedRuntime`, adjust `query()` options per mode, add turn-in-flight guard, per-turn timeout |
| `src/chat/types.ts` | Add `ResolvedRuntime` to `AgentConfig` |
| `src/index.ts` | Resolve runtime at startup, inject into engine, set env vars once |
| `src/events/types.ts` | Add `mode` and `model` to `turn.completed` and `turn.started` event data |

## Design Decisions

### Environment Variable Injection

The Agent SDK spawns Claude Code as a subprocess. The subprocess reads `ANTHROPIC_BASE_URL` and
`ANTHROPIC_API_KEY` from its inherited `process.env`.

Env vars are set **once at startup** based on the resolved mode. They are only mutated again
during a mode switch (Phase 6), which is guarded by the turn-in-flight counter.

```typescript
// In src/index.ts, after resolving startup mode:
function applyRuntimeEnv(runtime: ResolvedRuntime): void {
  if (runtime.mode === "offline") {
    process.env.ANTHROPIC_BASE_URL = runtime.baseUrl;
    process.env.ANTHROPIC_API_KEY = runtime.apiKey;
    // Suppress non-essential traffic from the Claude Code subprocess.
    // The subprocess may attempt telemetry to api.anthropic.com even when
    // the base URL points elsewhere.
    process.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1";
  } else {
    delete process.env.ANTHROPIC_BASE_URL;
    process.env.ANTHROPIC_API_KEY = runtime.apiKey;
    delete process.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC;
  }
}
```

**Why mutate `process.env` instead of passing `env` to `query()`?** The SDK's `env` option
**replaces** `process.env` entirely -- it does not merge. Passing a partial `env` strips `PATH`,
`HOME`, `DATABASE_URL`, and everything else from the subprocess. You must spread:
`{ ...process.env, ANTHROPIC_BASE_URL: url }`. But any future code that passes `env` without
the spread breaks everything. Mutating `process.env` once at startup is simpler and the SDK
defaults to `{...process.env}` when no `env` option is provided.

### Claude Code Non-First-Party Behavior

When `ANTHROPIC_BASE_URL` points at a non-Anthropic host, Claude Code's internal `MM()` function
returns `false`. This disables:

- **Prompt caching headers** -- correct, Ollama doesn't support them
- **ToolSearch** (a built-in tool) -- not used by Theo
- **Some telemetry** -- suppressed by `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`

These are benign degradations. The core agent loop, tool calling, hooks, and streaming are
unaffected.

### Turn-in-Flight Guard

Mode switches (Phase 6) must not mutate env vars while a `query()` is active. A simple counter
prevents this:

```typescript
class ChatEngine {
  private turnsInFlight = 0;
  private pendingModeSwitch: ResolvedRuntime | null = null;

  async handleMessage(body: string, gate: string): Promise<TurnResult> {
    this.turnsInFlight++;
    try {
      // ... existing handleMessage logic ...
      return result;
    } finally {
      this.turnsInFlight--;
      // Apply queued mode switch after turn completes
      if (this.turnsInFlight === 0 && this.pendingModeSwitch) {
        const pending = this.pendingModeSwitch;
        this.pendingModeSwitch = null;
        await this.applyModeSwitch(pending);
      }
    }
  }

  async switchMode(newRuntime: ResolvedRuntime): Promise<void> {
    if (this.turnsInFlight > 0) {
      this.pendingModeSwitch = newRuntime;
      return; // Applied after current turn completes
    }
    await this.applyModeSwitch(newRuntime);
  }

  // Actual switch logic -- only called when turnsInFlight === 0
  private async applyModeSwitch(newRuntime: ResolvedRuntime): Promise<void> {
    const oldSession = this.sessions.releaseSession("mode_switch");
    this.runtime = newRuntime;
    applyRuntimeEnv(newRuntime);
    await this.bus.emit({
      type: "system.mode.switched",
      version: 1,
      actor: "system",
      data: {
        from: oldSession ? this.runtime.mode : "unknown",
        to: newRuntime.mode,
        model: newRuntime.model,
        reason: "api_unreachable",
      },
      metadata: {},
    });
  }
}
```

### Query Options Per Mode

```typescript
const options = {
  model: this.runtime.model,
  systemPrompt,
  settingSources: [],
  mcpServers: { memory: this.memoryServer },
  allowedTools: ["mcp__memory__*"],
  resume: needsNew ? undefined : sessionId,
  persistSession: true,
  includePartialMessages: true,
  hooks: buildHooks(sessionId, this.bus, this.deps.episodic),
  // Mode-specific options
  ...(this.runtime.mode === "online"
    ? {
        thinking: { type: "adaptive" as const },
        maxBudgetUsd: this.config.maxBudgetPerTurn ?? 0.50,
      }
    : {
        // Explicitly disable thinking. If omitted, the subprocess may default
        // to adaptive thinking for unknown models, which Ollama won't handle.
        thinking: { type: "disabled" as const },
        // No budget -- local inference is free
        // Cap turns to prevent tool-retry loops from a weaker model
        maxTurns: this.config.OFFLINE_MAX_TURNS,
      }),
};
```

Key differences:

| Option | Online | Offline |
| ------ | ------ | ------- |
| `model` | `"claude-sonnet-4-6"` | `"qwen3.5:9b"` (from config) |
| `thinking` | `{ type: "adaptive" }` | `{ type: "disabled" }` |
| `maxBudgetUsd` | `0.50` | Omitted (free) |
| `maxTurns` | Unlimited | `10` (from config) |
| Everything else | Same | Same |

### Per-Turn Timeout

Ollama can stall under memory pressure (M1 16GB is tight). A per-turn timeout prevents the
process from hanging indefinitely:

```typescript
async handleMessage(body: string, gate: string): Promise<TurnResult> {
  this.turnsInFlight++;
  try {
    // ... session setup, prompt assembly ...

    const generator = query({ prompt: body, options });

    // Timeout only for offline mode -- Anthropic has its own timeouts
    const timeoutMs =
      this.runtime.mode === "offline" ? this.config.OFFLINE_TURN_TIMEOUT_MS : 0;
    let timeoutId: ReturnType<typeof setTimeout> | undefined;
    if (timeoutMs > 0) {
      timeoutId = setTimeout(() => {
        generator.return(undefined); // Kill the generator
      }, timeoutMs);
    }

    try {
      for await (const message of generator) {
        // ... existing message handling ...
      }
    } finally {
      if (timeoutId) clearTimeout(timeoutId);
    }

    // ... emit turn.completed ...
  } finally {
    this.turnsInFlight--;
    // ... pending mode switch check ...
  }
}
```

If the timeout fires, `generator.return()` terminates the async generator. The `for await` loop
exits, and the engine emits a `turn.failed` event with `errorType: "timeout"`.

### Token and Cost Accounting

Ollama estimates tokens as `content_length / 4` (approximate). Cost is always 0 offline.

```typescript
case "result":
  if (message.subtype === "success") {
    responseBody = message.result;
    inputTokens = message.usage?.input_tokens ?? 0;
    outputTokens = message.usage?.output_tokens ?? 0;
    costUsd = this.runtime.mode === "online" ? message.total_cost_usd : 0;
  }
  break;
```

### Event Metadata

`turn.started` and `turn.completed` gain `mode` and `model` fields:

```typescript
await this.bus.emit({
  type: "turn.completed",
  version: 1,
  actor: "theo",
  data: {
    sessionId,
    responseBody,
    durationMs,
    inputTokens,
    outputTokens,
    totalTokens: inputTokens + outputTokens,
    costUsd,
    mode: this.runtime.mode,
    model: this.runtime.model,
  },
  metadata: { sessionId },
});
```

These fields are required (not optional). Since Foundation is complete but pre-production, there
are no old events to worry about. Coordinate this type change with Foundation Phase 10's
`TurnCompletedData`.

### Session Handling

Sessions work the same in both modes. One rule: never resume a session across mode switches.
Sessions created with one model contain conversation history referencing that model's tool-call
patterns. Releasing the session and starting fresh ensures clean context.

## Definition of Done

- [ ] `ChatEngine` accepts `ResolvedRuntime` and adjusts `query()` options per mode
- [ ] Env vars set once at startup via `applyRuntimeEnv()`
- [ ] `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` set in offline mode
- [ ] Offline mode: `thinking: { type: "disabled" }` (explicit, not omitted)
- [ ] Offline mode: `maxTurns` set from config (default 10)
- [ ] Offline mode: per-turn timeout (default 120s) kills the generator on expiry
- [ ] Offline mode: `costUsd` recorded as `0` in `turn.completed`
- [ ] Turn-in-flight guard prevents `switchMode()` during active query
- [ ] Pending mode switch applied after turn completes
- [ ] `turn.started` and `turn.completed` events include `mode` and `model`
- [ ] `src/events/types.ts` updated with new required fields
- [ ] Existing tests still pass (online mode unchanged)
- [ ] `just check` passes

## Test Cases

### `tests/chat/engine.test.ts` (additions)

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Online options | `mode: "online"` | `thinking: adaptive`, `maxBudgetUsd`, no `maxTurns` |
| Offline options | `mode: "offline"` | `thinking: disabled`, no budget, `maxTurns: 10` |
| Offline cost | Offline turn completes | `costUsd: 0` |
| Event fields | Either mode | `turn.completed` has `mode` and `model` |
| Turn-in-flight | Call `switchMode()` during active turn | Switch is queued, not applied |
| Queued switch | Turn completes with pending switch | `applyModeSwitch()` called, env updated |
| Timeout | Offline, generator stalls past timeout | `turn.failed` with `errorType: "timeout"` |

Engine tests mock `query()`. They verify options passed, not actual responses.

## Risks

**Medium-High risk.** Three concerns:

1. **Env var mutation**: Safe because Theo is single-user, single-process, and the turn-in-flight
   guard serializes mutations. If Theo ever becomes multi-process, env vars must be replaced with
   per-query `env` option (with full `process.env` spread).

2. **`generator.return()` behavior**: The SDK docs do not specify whether `generator.return()`
   cleanly terminates the subprocess. If it does not, the subprocess may leak. The Phase 1 smoke
   test (Test 5) should verify subprocess cleanup.

3. **Ollama SSE fidelity**: The SDK's stream parser enforces strict event ordering. If Ollama
   sends events out of order, the parser throws. Phase 1's streaming test catches gross
   violations, but edge cases may surface with longer conversations.
