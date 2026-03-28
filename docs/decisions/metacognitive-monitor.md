# Metacognitive monitor

## Context

The deliberative reasoning engine (FPL-36) runs multi-step background reasoning with 5 phases. Without oversight, deliberation can exhibit pathological patterns: spinning in circles, drifting from the question, claiming confidence without evidence, or producing no new information. The metacognitive monitor detects these patterns and intervenes.

## Decisions

### Pure function, not a service

The monitor is a stateless async function called between phases. No background process, no persistent state, no subscriptions. The deliberation engine owns the call site and handles the returned decision. This avoids coupling and makes testing trivial.

**Rationale**: Metacognition is a checkpoint, not an autonomous process. Keeping it stateless means no lifecycle management, no cleanup, and no race conditions with the deliberation itself.

### Four detection patterns with embedding-based analysis

1. **Spinning**: cosine similarity between consecutive phase outputs > threshold (default 0.85). If phase N says essentially the same thing as phase N-1, the deliberation is stuck.

2. **Scope drift**: cosine similarity between the current phase output and the original question < threshold (default 0.7). If the output has diverged from what was asked, the deliberation has wandered.

3. **Overconfidence**: phase claims high confidence (keyword matching) but references fewer distinct memory nodes than the minimum evidence threshold (default 3). Only checked in evaluate/synthesize phases.

4. **Diminishing returns**: current phase references no novel memory nodes beyond what prior phases already found. Only checked after gather phase.

**Rationale**: Spinning and drift use the existing `Embedder` singleton with L2-normalized vectors (dot product = cosine similarity). No new infrastructure needed. Overconfidence and diminishing returns use simple heuristics on node IDs extracted from phase text. The detection order is deliberate: spinning > drift > overconfidence > diminishing returns, checked in priority order.

### Node ID extraction via regex

Memory node IDs are extracted from phase output text by matching `"id": <int>` and `"node_id": <int>` patterns. This is approximate but sufficient for heuristic checks, since the LLM in gather phase echoes tool result JSON.

**Rationale**: `stream_and_collect` doesn't expose which node IDs were referenced. Adding that coupling would be more invasive than a simple regex. The heuristic is adequate for overconfidence and diminishing returns detection.

### Four intervention types

- **Continue**: no pathology, proceed normally
- **Redirect**: inject constraints into the next phase (via redirect prompt stored in phase_outputs)
- **Escalate**: publish `MetacognitionAlert` event (ephemeral) for Telegram notification; deliberation continues
- **Abort**: complete the deliberation early with whatever output is available

**Rationale**: Redirect fixes mild issues in-flight. Escalation notifies the owner without blocking (the monitor's assessment might be wrong). Abort is for clear waste of resources. No intervention type crashes the deliberation.

### Monitor failure is non-fatal

If the monitor raises any exception, the deliberation continues as if the check returned "continue". Metacognition is advisory, not a gate.

**Rationale**: A monitoring failure should never prevent a deliberation from completing. The monitor catches its own exceptions and logs warnings.

### MetacognitionAlert is ephemeral

The alert event is not persisted to the event store. It exists only to trigger a Telegram notification when a subscriber is registered.

**Rationale**: Alert history can be reconstructed from OTEL metrics and logs. Persisting every check result would add write overhead with little value.

### Metacognition is opt-out

Enabled by default (`metacognition_enabled: bool = True`). All thresholds are configurable via `THEO_METACOGNITION_*` env vars.

## Files changed

- `src/theo/conversation/metacognition.py` — new module: detection functions, monitor entry point, OTEL metrics
- `src/theo/conversation/deliberation.py` — integration: call monitor after each phase, handle decisions
- `src/theo/config.py` — 4 new settings (enabled, spinning threshold, drift threshold, min evidence)
- `src/theo/bus/events.py` — `MetacognitionAlert` ephemeral event
- `tests/test_metacognition.py` — 41 tests covering all detection patterns, integration, and edge cases
- `tests/test_deliberation_engine.py` — updated settings mock to include metacognition_enabled
