# Theo Offline Mode -- Overview

6 phases. Each is incremental, testable, and completable in one context window.

**Prerequisite:** All 16 Foundation phases are complete. Theo has a working agent runtime,
memory system, MCP tools, scheduler, and CLI gate.

**Requires:** Ollama >= 0.14.3 (fixes tool-call streaming timeout and `count_tokens` 404
handling).

## Why

Theo should work without an internet connection. When the Anthropic API is unreachable -- airplane
mode, API outage, or deliberate offline use -- Theo falls back to a local model via Ollama. The
agent loop, memory tools, and event system stay identical. The model gets smaller, some features
are gated, but Theo remains functional.

## How It Works

Ollama v0.14.3+ implements the Anthropic Messages API (`/v1/messages`), including tool use and
streaming. The Claude Agent SDK spawns Claude Code as a subprocess that reads
`ANTHROPIC_BASE_URL` from `process.env`. No proxy, no provider abstraction, no new agent loop:

```text
Agent SDK query()
  --> Claude Code subprocess (reads process.env)
    --> ANTHROPIC_BASE_URL=http://localhost:11434
      --> Ollama /v1/messages?beta=true
        --> Qwen3.5-9B (local, ~20-30 tok/s on M1 16GB)
```

**Important nuance:** Claude Code is a full application (~13MB), not a thin API wrapper. It
detects non-Anthropic base URLs via an internal `MM()` function and adapts:

- Prompt caching headers: disabled (correct -- Ollama doesn't support them)
- ToolSearch: disabled (not needed by Theo)
- Telemetry: may fail silently (harmless)
- Cost tracker: returns 0 for unknown models (correct for offline)

These are benign degradations, not failures. The subprocess path is well-tested by the Claude
Code + Ollama community.

## Target Hardware

Mac Mini M1, 16GB unified RAM. This constrains the model to ~9B dense parameters or ~3B active
(MoE).

### Memory Budget (LFM2.5-1.2B-Instruct)

| Component | Estimated Usage |
| --------- | --------------- |
| LFM2.5-1.2B model weights | < 1.0 GB |
| KV cache | ~0.1 GB |
| PostgreSQL | ~1.0 GB |
| Bun process + ONNX embeddings | ~0.5 GB |
| macOS + system services | ~4.0 GB |
| **Total** | **~6.6 GB** |
| **Headroom** | **~9.4 GB** |

The sub-1GB model footprint eliminates all memory pressure concerns. ONNX embeddings stay
loaded, PostgreSQL runs comfortably, and there is ample headroom for macOS file cache and
other processes. No OOM risk, no need to tune `num_parallel` or context limits.

## Dependency Graph

```text
Phase 1: Ollama Infrastructure (install, model pull, SDK subprocess validation)
    |
Phase 2: Config Extension (mode toggle, conditional API key, model selection)
    |
Phase 3: Engine Adaptation (env var injection, query options, turn safety)
    |
    +--------+--------+
    |                 |
Phase 4           Phase 5
Prompt              Feature
Optimization        Gating
    |                 |
    +--------+--------+
             |
Phase 6: Auto-Detection & Fallback (health checks, graceful switching)
```

## Phase Summary

| # | Phase | Key Deliverables | Est. Lines | Risk |
| --- | ------- | ------------------ | ----------- | ------ |
| 1 | Ollama Infrastructure | justfile recipes, model pull, beta API + SDK subprocess + tool calling validation | ~200 | Medium |
| 2 | Config Extension | `RuntimeMode` type, conditional `ANTHROPIC_API_KEY`, Ollama config | ~200 | Low |
| 3 | Engine Adaptation | Mode-aware query options, turn-in-flight guard, per-turn timeout, env injection | ~500 | Medium-High |
| 4 | Prompt Optimization | Shorter system prompts, structured tool guidance, token budget reduction | ~300 | Medium |
| 5 | Feature Gating | Disable background intelligence offline, limit scheduler, maxTurns, MCP validation | ~300 | Low |
| 6 | Auto-Detection & Fallback | Health checks with auth handling, guarded mode switching, AbortController cleanup | ~500 | High |

**Total: ~2,000 lines across 6 phases.**

## Systemic Decisions

### 1. Same SDK, Different Endpoint

The Agent SDK's `query()` is the only model interface. Offline mode does not introduce a provider
abstraction, a new agent loop, or an alternative SDK. It changes three things: the base URL, the
API key, and the model name. Everything else -- hooks, MCP tools, sessions, streaming -- flows
through the same code path.

The Claude Code subprocess calls the **beta** Messages API (`/v1/messages?beta=true`). Phase 1
validates that Ollama handles this path correctly.

### 2. Mode as Config, Not Architecture

Runtime mode (`online`, `offline`, `auto`) is a configuration value, not a code branch. The
`ChatEngine` reads the mode at query time and adjusts options accordingly. There is no
`OfflineChatEngine` subclass. One engine, one code path, mode-dependent parameters.

### 3. Degraded, Not Broken

Offline mode disables features that require strong reasoning (background intelligence, autonomous
scheduling) rather than running them poorly. A 9B model hallucinating tool arguments in a
background job is worse than skipping the job. Offline turns are capped at 10 SDK turns to
prevent tool-retry loops from a weaker model.

### 4. Events Record Everything

Every turn records which mode and model were used. The event log becomes the audit trail for
offline vs online behavior, enabling future analysis of quality differences.

### 5. Turn Safety

Mode switches are guarded by a turn-in-flight counter. `switchMode()` cannot mutate env vars
while a `query()` is active -- it queues the switch and applies it after the current turn
completes.

### 6. Model Recommendation

Based on April 2026 benchmarks. The plan is model-agnostic -- any Ollama-compatible model works.

| Model | Active Params | Speed (M1 16GB) | Tool Calling | Fit |
| ----- | ------------- | ---------------- | ------------ | --- |
| **LFM2-24B-A2B** | **2B (MoE)** | **~385ms/tool call** | **80% accuracy** | **Best for agents, tight on 16GB** |
| Qwen3.5-9B | 9B | ~20-30 tok/s | Good | Good balance |
| Qwen3-8B | 8B | ~25-35 tok/s | Strong | Alternative |
| LFM2.5-1.2B | 1.2B | Very fast | Trained for agents | Too small for conversation |
| Qwen3.5-35B-A3B | 3B (MoE) | ~17 tok/s | Good | Tight on 16GB |

**Primary recommendation: LFM2-24B-A2B.** Liquid AI's MoE model is purpose-built for on-device
tool-calling agents. With only 2B active parameters it is fast, and 80% tool-selection accuracy
is the best in its class. It requires ~14.5GB RAM, which is tight on 16GB -- disable the ONNX
embedding model while offline (Ollama can handle embeddings too) or use Q4 quantization.

**Fallback: Qwen3.5-9B.** If LFM2 is too memory-hungry, Qwen3.5-9B at ~5.5GB is the safe
choice with plenty of headroom.

### 7. Backend: LM Studio + MLX (Recommended)

The plan works with any local server that implements the Anthropic Messages API. The config
field is `LOCAL_MODEL_BASE_URL` (named generically, not Ollama-specific).

- **LM Studio + MLX** -- Recommended for Apple Silicon. Native Anthropic API compatibility
  since v0.4.1. MLX backend uses ~50% less memory and ~2x faster inference than llama.cpp.
  Continuous batching (v0.4.2) enables parallel tool calls. Default port: `localhost:1234`.
- **Ollama + MLX** -- Headless service, best for always-on Theo. Adopted MLX backend in
  March 2026. Default port: `localhost:11434`.
- **llama.cpp server** -- Bare metal, maximum control.

### 8. Model: LFM2.5-1.2B-Instruct

| Model | Params | RAM | Tool Use (BFCLv3) | Notes |
| ----- | ------ | --- | ----------------- | ----- |
| **LFM2.5-1.2B-Instruct** | **1.2B** | **< 1 GB** | **49** | **Default. Fast, minimal footprint** |
| LFM2.5-1.2B-Thinking | 1.2B | < 1 GB | 57 | Higher tool accuracy, slower (reasoning traces) |
| LFM2-24B-A2B | 2B active | ~14.5 GB | 80% | Best quality, tight on 16GB |

The default is LFM2.5-1.2B-Instruct. It is the fastest option with < 1GB memory, leaving
maximum headroom for PostgreSQL, embeddings, and the OS. The Instruct variant is preferred over
Thinking because the reasoning traces add latency without proportional quality gains at 1.2B
scale. Both are from Liquid AI, purpose-built for on-device agents with tool calling.

Users who want higher tool accuracy can switch to `lfm2:24b` via `LOCAL_MODEL`.

## Event Catalog (New Events)

| Event Type | Phase | Actor | Purpose |
| ------------ | ------- | ------- | --------- |
| `system.mode.switched` | 6 | system | Runtime mode changed (online <-> offline) |

Existing `turn.started` and `turn.completed` events gain `mode` and `model` fields in their
data (Phase 3). Existing `job.cancelled` gains a `reason` field (Phase 5).

## What "done" means

Theo works without internet. The user starts Theo with `--offline` or Theo auto-detects API
unavailability. Theo connects to a local Ollama instance, uses Qwen3.5-9B (or configured model),
handles conversations with memory retrieval and storage, streams responses through the CLI, and
records all activity in the event log with mode annotations. Background intelligence and
autonomous scheduling are cleanly disabled. Offline turns are capped at 10 turns and 120 seconds
to prevent runaway loops. When the API becomes available again, Theo can switch back to Claude
with no data loss.
