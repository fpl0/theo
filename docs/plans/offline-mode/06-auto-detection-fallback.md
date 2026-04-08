# Phase 6: Auto-Detection & Graceful Fallback

## Motivation

The `auto` runtime mode promises seamless switching: use Claude when available, fall back to
Ollama when it is not. This phase implements the health checking, mode switching, and event
recording that makes `auto` mode work. It also adds the `--offline` CLI flag for explicit offline
use without changing environment variables.

## Depends on

- **Phase 3** -- Engine accepts `ResolvedRuntime` with turn-in-flight guard
- **Phase 4** -- Prompt optimization for offline mode
- **Phase 5** -- Feature gating for offline mode
- **Foundation Phase 11** -- CLI gate exists

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/health/checks.ts` | Health check functions for Anthropic API and Ollama |
| `tests/health/checks.test.ts` | Health check tests |

### Files to modify

| File | Change |
| ------ | -------- |
| `src/index.ts` | Run health checks at startup, resolve mode, warm up model, start recheck loop |
| `src/cli/gate.ts` | Add `--offline` flag parsing, `/online` and `/offline` commands |
| `src/events/types.ts` | Add `system.mode.switched` event type |

## Design Decisions

### Health Checks

Two checks, each with short timeouts and specific error handling:

```typescript
// src/health/checks.ts

interface HealthResult {
  readonly reachable: boolean;
  readonly latencyMs: number;
  readonly error?: string;
}

async function checkAnthropic(apiKey: string): Promise<HealthResult> {
  const start = Date.now();
  try {
    // Use a minimal authenticated request. Any non-5xx response means
    // the API is reachable. Handle 401 specifically -- an expired key
    // is not "reachable" from an operational perspective.
    const response = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        model: "claude-haiku-4-5-20251001",
        max_tokens: 1,
        messages: [{ role: "user", content: "." }],
      }),
      signal: AbortSignal.timeout(5000),
    });
    const latencyMs = Date.now() - start;

    // 401 = auth failure. The API is "up" but we can't use it.
    if (response.status === 401) {
      return { reachable: false, latencyMs, error: "Authentication failed (401)" };
    }
    // 5xx = server error. API is unhealthy.
    if (response.status >= 500) {
      return { reachable: false, latencyMs, error: `Server error (${response.status})` };
    }
    // 200, 400, 429 = API is reachable and authenticated
    return { reachable: true, latencyMs };
  } catch (e) {
    return {
      reachable: false,
      latencyMs: Date.now() - start,
      error: e instanceof Error ? e.message : String(e),
    };
  }
}

async function checkOllama(
  baseUrl: string,
  model?: string,
): Promise<HealthResult & { modelAvailable?: boolean }> {
  const start = Date.now();
  try {
    // Step 1: Is Ollama running?
    const response = await fetch(baseUrl, {
      signal: AbortSignal.timeout(3000),
    });
    if (!response.ok) {
      return { reachable: false, latencyMs: Date.now() - start, error: "Not OK" };
    }

    // Step 2: Is the model pulled?
    let modelAvailable: boolean | undefined;
    if (model) {
      try {
        const tags = await fetch(`${baseUrl}/api/tags`, {
          signal: AbortSignal.timeout(3000),
        });
        if (tags.ok) {
          const data = (await tags.json()) as {
            models?: Array<{ name: string }>;
          };
          modelAvailable = data.models?.some((m) =>
            m.name.startsWith(model.split(":")[0]),
          ) ?? false;
        }
      } catch {
        // Non-critical -- model check is best-effort
      }
    }

    return { reachable: true, latencyMs: Date.now() - start, modelAvailable };
  } catch (e) {
    return {
      reachable: false,
      latencyMs: Date.now() - start,
      error: e instanceof Error ? e.message : String(e),
    };
  }
}
```

### Model Warm-Up

On M1 16GB, cold-start model loading takes 10-30 seconds. After confirming Ollama is reachable,
send a minimal request to trigger model loading before the first conversation:

```typescript
async function warmUpModel(baseUrl: string, model: string): Promise<void> {
  console.log(`[offline] Loading model ${model}...`);
  try {
    const client = new Anthropic({ baseURL: baseUrl, apiKey: "ollama" });
    await client.messages.create({
      model,
      max_tokens: 1,
      messages: [{ role: "user", content: "." }],
    });
    console.log(`[offline] Model loaded.`);
  } catch (e) {
    console.log(`[offline] Model warm-up failed: ${e instanceof Error ? e.message : e}`);
    // Non-fatal -- first real request will trigger loading instead
  }
}
```

### Startup Mode Resolution

```typescript
async function resolveStartupMode(config: Config): Promise<Result<ResolvedRuntime>> {
  switch (config.RUNTIME_MODE) {
    case "online": {
      const health = await checkAnthropic(config.ANTHROPIC_API_KEY!);
      if (!health.reachable) {
        return err({
          code: "API_UNREACHABLE",
          message: health.error ?? "Anthropic API unreachable",
        });
      }
      return ok(buildOnlineRuntime(config));
    }

    case "offline": {
      const health = await checkOllama(config.LOCAL_MODEL_BASE_URL, config.LOCAL_MODEL);
      if (!health.reachable) {
        return err({
          code: "OLLAMA_UNAVAILABLE",
          message: health.error ?? "Ollama unreachable",
          url: config.LOCAL_MODEL_BASE_URL,
        });
      }
      if (health.modelAvailable === false) {
        return err({
          code: "OLLAMA_UNAVAILABLE",
          message: `Model ${config.LOCAL_MODEL} not found. Run: ollama pull ${config.LOCAL_MODEL}`,
          url: config.LOCAL_MODEL_BASE_URL,
        });
      }
      await warmUpModel(config.LOCAL_MODEL_BASE_URL, config.LOCAL_MODEL);
      return ok(buildOfflineRuntime(config));
    }

    case "auto": {
      const [apiHealth, ollamaHealth] = await Promise.all([
        config.ANTHROPIC_API_KEY
          ? checkAnthropic(config.ANTHROPIC_API_KEY)
          : Promise.resolve({ reachable: false, latencyMs: 0, error: "No key" }),
        checkOllama(config.LOCAL_MODEL_BASE_URL, config.LOCAL_MODEL),
      ]);

      if (apiHealth.reachable) {
        return ok(buildOnlineRuntime(config));
      }
      if (ollamaHealth.reachable) {
        console.log("[auto] Anthropic API unreachable, falling back to Ollama");
        await warmUpModel(config.LOCAL_MODEL_BASE_URL, config.LOCAL_MODEL);
        return ok(buildOfflineRuntime(config));
      }
      return err({
        code: "NO_MODEL_AVAILABLE",
        message: "Neither Anthropic API nor Ollama is reachable",
      });
    }
  }
}
```

### Runtime Mode Switching

Mode switches are user-initiated, never automatic mid-conversation:

1. Online `query()` fails with network error
2. CLI informs user: "Anthropic API is unreachable. Switch to offline? [y/N]"
3. User confirms
4. Engine's `switchMode()` queues the switch (turn-in-flight guard from Phase 3)
5. After current turn completes/fails, the switch is applied
6. User must re-send their message (the failed turn's context is lost)

The user's last message is safe -- it was already persisted as a `message.received` event.

### CLI Commands

```typescript
// In src/cli/gate.ts:

// Startup flag
const args = process.argv.slice(2);
if (args.includes("--offline")) {
  process.env.RUNTIME_MODE = "offline";
}

// Runtime commands
function registerModeCommands(cli: CliGate, engine: ChatEngine, config: Config): void {
  cli.command("/offline", async () => {
    const health = await checkOllama(config.LOCAL_MODEL_BASE_URL, config.LOCAL_MODEL);
    if (!health.reachable) {
      cli.print("[error] Ollama is not reachable");
      return;
    }
    await warmUpModel(config.LOCAL_MODEL_BASE_URL, config.LOCAL_MODEL);
    await engine.switchMode(buildOfflineRuntime(config));
    cli.print("[offline] Switched to local model");
  });

  cli.command("/online", async () => {
    if (!config.ANTHROPIC_API_KEY) {
      cli.print("[error] No ANTHROPIC_API_KEY configured");
      return;
    }
    const health = await checkAnthropic(config.ANTHROPIC_API_KEY);
    if (!health.reachable) {
      cli.print(`[error] Anthropic API unreachable: ${health.error}`);
      return;
    }
    await engine.switchMode(buildOnlineRuntime(config));
    cli.print("[online] Switched to Claude");
  });
}
```

### Mode Switch Event

```typescript
| {
    readonly type: "system.mode.switched";
    readonly version: 1;
    readonly actor: "system";
    readonly data: {
      readonly from: "online" | "offline";
      readonly to: "online" | "offline";
      readonly model: string;
      readonly reason: string;
    };
  }
```

### Periodic Health Re-Check (Auto Mode)

When in auto mode with offline fallback active, periodically check if the Anthropic API is back.
Uses `AbortController` for clean shutdown, configurable interval with exponential backoff on
repeated failures:

```typescript
const DEFAULT_RECHECK_MS = 5 * 60 * 1000; // 5 minutes
const MAX_RECHECK_MS = 60 * 60 * 1000;    // 1 hour

function startApiRecheck(
  config: Config,
  engine: ChatEngine,
  bus: EventBus,
  signal: AbortSignal,
): void {
  let intervalMs = DEFAULT_RECHECK_MS;
  let consecutiveFailures = 0;

  const loop = async () => {
    while (!signal.aborted) {
      await new Promise((r) => setTimeout(r, intervalMs));
      if (signal.aborted) break;
      if (engine.currentMode !== "offline") {
        // Already online -- reset and keep watching
        consecutiveFailures = 0;
        intervalMs = DEFAULT_RECHECK_MS;
        continue;
      }

      const health = await checkAnthropic(config.ANTHROPIC_API_KEY!);
      if (health.reachable) {
        consecutiveFailures = 0;
        intervalMs = DEFAULT_RECHECK_MS;
        bus.emitEphemeral({
          type: "stream.chunk",
          data: {
            text: "\n[auto] Anthropic API is reachable. Use /online to switch.\n",
            sessionId: "system",
          },
        });
      } else {
        consecutiveFailures++;
        // Exponential backoff: 5m -> 10m -> 20m -> 40m -> 60m (capped)
        intervalMs = Math.min(
          DEFAULT_RECHECK_MS * 2 ** consecutiveFailures,
          MAX_RECHECK_MS,
        );
      }
    }
  };

  void loop();
}
```

Usage in startup:

```typescript
// In src/index.ts:
const shutdownController = new AbortController();

// ... after resolving mode ...
if (config.RUNTIME_MODE === "auto") {
  startApiRecheck(config, engine, bus, shutdownController.signal);
}

// In shutdown handler:
process.on("SIGTERM", async () => {
  shutdownController.abort(); // Stops recheck loop
  await pool.end();
  process.exit(0);
});
```

### Ollama Configuration Recommendations

Document in model-eval.md:

- `OLLAMA_KEEP_ALIVE=24h` -- Keep model loaded between requests (default is 5 minutes,
  causing re-load latency on idle conversations)
- `OLLAMA_NUM_PARALLEL=1` -- Prevent Ollama from loading multiple model instances
- `OLLAMA_MAX_LOADED_MODELS=1` -- Keep memory bounded

## Definition of Done

- [ ] `checkAnthropic()` handles 200 (reachable), 401 (not reachable), 5xx (not reachable)
- [ ] `checkOllama()` verifies server is running AND model is pulled
- [ ] `warmUpModel()` triggers model loading before first conversation
- [ ] Startup `resolveStartupMode()` handles all three modes correctly
- [ ] `--offline` CLI flag overrides `RUNTIME_MODE`
- [ ] `/online` and `/offline` CLI commands switch mode at runtime
- [ ] Mode switch via CLI waits for turn-in-flight guard (Phase 3)
- [ ] `system.mode.switched` event typed in the event union
- [ ] Periodic recheck uses `AbortController`, cleaned up on shutdown
- [ ] Recheck uses exponential backoff (5m -> 10m -> 20m -> 60m cap)
- [ ] User notified when API becomes available again
- [ ] `just check` passes

## Test Cases

### `tests/health/checks.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Anthropic 200 | Mock 200 | `reachable: true` |
| Anthropic 401 | Mock 401 | `reachable: false`, error mentions auth |
| Anthropic 500 | Mock 500 | `reachable: false` |
| Anthropic timeout | Mock slow response | `reachable: false` within 5s |
| Anthropic network error | Mock connection refused | `reachable: false` |
| Ollama reachable | Mock 200 on base URL | `reachable: true` |
| Ollama model check | Mock `/api/tags` with model | `modelAvailable: true` |
| Ollama model missing | Mock `/api/tags` without model | `modelAvailable: false` |
| Ollama down | Mock connection refused | `reachable: false` |

### `tests/chat/engine.test.ts` (additions)

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Switch queued | `switchMode()` during turn | Pending, applied after turn |
| /offline command | Ollama reachable | Mode switches to offline |
| /online command | API reachable | Mode switches to online |

### CLI flag tests

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| `--offline` | Start with flag | `RUNTIME_MODE` = `"offline"` |
| No flag | Start normally | `RUNTIME_MODE` from env |

### Recheck tests

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Backoff | 3 failures | Interval doubles each time |
| Recovery | API comes back | Interval resets, user notified |
| Abort | Signal aborted | Loop exits cleanly |

## Risks

**High risk.** This is the most complex phase. Key concerns:

1. **Health check false positive**: API returns 200 but subsequent `query()` fails (transient
   issue). Mitigation: the engine's error handling catches query failures independently. The
   health check is a hint, not a guarantee.

2. **Health check cost**: ~$0.01/day with haiku `max_tokens: 1` at 5-minute intervals. Negligible
   but not zero. The exponential backoff reduces frequency during extended outages.

3. **Model warm-up blocks startup**: 10-30 seconds for cold-start loading. Mitigation: the
   warm-up prints a "Loading model..." message so the user knows what's happening.

4. **Mode switch loses in-flight context**: The current turn's response is lost. The user must
   re-send their message. Mitigation: the user's message is persisted in the event log. Future
   enhancement: auto-resubmit the last message after mode switch.
