# Phase 2: Config Extension

## Motivation

Theo's config currently requires `ANTHROPIC_API_KEY` unconditionally. For offline mode, the API
key is unnecessary -- Ollama uses a dummy key. This phase extends the config schema to support
runtime mode selection and conditional validation, so Theo knows whether to talk to Anthropic or
Ollama before the engine starts.

## Depends on

- **Phase 1** -- Ollama is installed and verified working

## Scope

### Files to modify

| File | Change |
| ------ | -------- |
| `src/config.ts` | Add `RUNTIME_MODE`, `LOCAL_MODEL`, `LOCAL_MODEL_BASE_URL`, `OFFLINE_TURN_TIMEOUT_MS`; make `ANTHROPIC_API_KEY` conditional |
| `src/errors.ts` | Add `OLLAMA_UNAVAILABLE` and `NO_MODEL_AVAILABLE` error variants |
| `tests/config.test.ts` | Tests for new config combinations |

## Design Decisions

### Runtime Mode

Three modes, configured via `RUNTIME_MODE` environment variable:

```typescript
const runtimeModeSchema = z.enum(["online", "offline", "auto"]).default("auto");
```

- **`online`** -- Always use Anthropic API. Fail if API key is missing. Current behavior.
- **`offline`** -- Always use Ollama. No API key needed. Fail if Ollama is unreachable.
- **`auto`** -- Try Anthropic first. If unreachable, fall back to Ollama. Requires API key.
  This is the default.

### Config Schema

```typescript
const configSchema = z
  .object({
    DATABASE_URL: z.string().url(),
    RUNTIME_MODE: runtimeModeSchema,

    // Anthropic (required for online/auto)
    ANTHROPIC_API_KEY: z.string().min(1).optional(),

    // Ollama
    LOCAL_MODEL_BASE_URL: z.string().url().default("http://localhost:1234"),
    LOCAL_MODEL: z.string().default("lfm2.5:1.2b"),

    // Offline safety
    OFFLINE_TURN_TIMEOUT_MS: z.coerce.number().default(120_000), // 2 min
    OFFLINE_MAX_TURNS: z.coerce.number().default(10),

    // Existing optional fields
    DB_POOL_MAX: z.coerce.number().default(10),
    DB_IDLE_TIMEOUT: z.coerce.number().default(30),
    DB_CONNECT_TIMEOUT: z.coerce.number().default(10),
    TELEGRAM_BOT_TOKEN: z.string().optional(),
    TELEGRAM_OWNER_ID: z.string().optional(),
  })
  .superRefine((data, ctx) => {
    if (data.RUNTIME_MODE !== "offline" && !data.ANTHROPIC_API_KEY) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["ANTHROPIC_API_KEY"],
        message: "Required when RUNTIME_MODE is 'online' or 'auto'",
      });
    }
  });
```

### Resolved Runtime Type

`ResolvedRuntime` represents an already-decided mode. It does not contain fallback logic -- that
lives in Phase 6's `resolveStartupMode()`. This type is a simple data carrier:

```typescript
interface ResolvedRuntime {
  readonly mode: "online" | "offline";
  readonly model: string;
  readonly baseUrl: string | undefined; // undefined = Anthropic default
  readonly apiKey: string | undefined;
}

function buildOnlineRuntime(config: Config): ResolvedRuntime {
  return {
    mode: "online",
    model: "claude-sonnet-4-6",
    baseUrl: undefined,
    apiKey: config.ANTHROPIC_API_KEY,
  };
}

function buildOfflineRuntime(config: Config): ResolvedRuntime {
  return {
    mode: "offline",
    model: config.LOCAL_MODEL,
    baseUrl: config.LOCAL_MODEL_BASE_URL,
    apiKey: "ollama",
  };
}
```

**Why no `resolveRuntime(config, ollamaReachable)` function:** An earlier draft had a combined
resolver that accepted a boolean flag and contained auto-mode fallback logic. This created dead
code paths and confused responsibilities. The decision of *which* mode to use belongs in Phase
6's startup health checks. Config only provides the building blocks.

### Error Variants

Add to `AppError` union:

```typescript
| {
    readonly code: "OLLAMA_UNAVAILABLE";
    readonly message: string;
    readonly url: string;
  }
| {
    readonly code: "NO_MODEL_AVAILABLE";
    readonly message: string;
  }
```

## Definition of Done

- [ ] `loadConfig()` with `RUNTIME_MODE=offline` succeeds without `ANTHROPIC_API_KEY`
- [ ] `loadConfig()` with `RUNTIME_MODE=online` fails without `ANTHROPIC_API_KEY`
- [ ] `loadConfig()` with `RUNTIME_MODE=auto` fails without `ANTHROPIC_API_KEY`
- [ ] Default `RUNTIME_MODE` is `"auto"` when not set
- [ ] Default `LOCAL_MODEL_BASE_URL` is `"http://localhost:1234"`
- [ ] Default `LOCAL_MODEL` is `"qwen3.5:9b"`
- [ ] Default `OFFLINE_TURN_TIMEOUT_MS` is `120000`
- [ ] Default `OFFLINE_MAX_TURNS` is `10`
- [ ] `buildOnlineRuntime()` and `buildOfflineRuntime()` return correct values
- [ ] `AppError` includes `OLLAMA_UNAVAILABLE` and `NO_MODEL_AVAILABLE` variants
- [ ] `just check` passes

## Test Cases

### `tests/config.test.ts` (additions)

| Test | Input | Expected |
| ------ | ------- | ---------- |
| Offline, no API key | `RUNTIME_MODE=offline`, no `ANTHROPIC_API_KEY` | `{ ok: true }` |
| Online, no API key | `RUNTIME_MODE=online`, no `ANTHROPIC_API_KEY` | `{ ok: false }` |
| Auto, no API key | `RUNTIME_MODE=auto`, no `ANTHROPIC_API_KEY` | `{ ok: false }` |
| Defaults applied | Only `DATABASE_URL` + `ANTHROPIC_API_KEY` | `RUNTIME_MODE === "auto"`, `LOCAL_MODEL === "lfm2.5:1.2b"` |
| Custom model | `LOCAL_MODEL=qwen3-8b` | `value.LOCAL_MODEL === "qwen3-8b"` |
| Invalid URL | `LOCAL_MODEL_BASE_URL=not-a-url` | `{ ok: false }` |
| Build online | Valid config | `mode: "online"`, `model: "claude-sonnet-4-6"` |
| Build offline | Valid config | `mode: "offline"`, `model: config.LOCAL_MODEL` |
| Timeout default | No `OFFLINE_TURN_TIMEOUT_MS` | `120000` |
| Max turns default | No `OFFLINE_MAX_TURNS` | `10` |

## Risks

**Low risk.** Config changes are well-contained. The `superRefine` approach is standard Zod.
Ensure `.env.local` examples are updated so users know about the new variables.
