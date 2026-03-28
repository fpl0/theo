# Deliberative Reasoning Engine

**Date:** 2026-03-28
**Ticket:** FPL-36

## Context

Theo needed the ability to reason deeply about complex questions rather than answering in a single turn. This is the centerpiece of M3's "Theo takes initiative" milestone. The deliberation store (FPL-30) provided the persistence layer; this ticket builds the reasoning orchestrator on top.

## Decisions

### Hardcoded phase progression, not LLM-controlled

The five phases (frame, gather, generate, evaluate, synthesize) execute in fixed order. Unlike onboarding — which has a human in the loop — deliberation runs in the background with no audience during phases. Fixed progression gives:
- Predictable cost (exactly 5 Opus calls worst case)
- Consistent span trees for debugging
- No risk of infinite loops

One exception: after `gather`, the LLM can signal early-exit (`[EARLY_EXIT]`) to skip directly to `synthesize` for questions that turn out to be simpler than expected.

### Shared stream-and-tool loop

Extracted `_stream_and_tool_loop` from `turn.py` into `conversation/stream.py` as `stream_and_collect()`. Both `execute_turn()` and `run_deliberation()` call the same helper with different parameters. Turn execution passes an `on_text` callback that publishes `ResponseChunk` events; deliberation runs silently (no callback). This avoids duplicating the stream→tool→stream logic.

### Only gather phase gets tools

The gather phase needs to search memory (via `search_memory`, `read_core_memory`), so it receives the full tool definitions. Other phases are pure reasoning — they receive prior phase outputs in the system prompt and produce text. Giving tools to frame/generate/evaluate/synthesize would add cost and complexity for no benefit.

### Background execution with session lock release

The deliberation spawns as an `asyncio.create_task()` from the `start_deliberation` tool handler. The turn that triggered it completes normally (releasing the session lock), so the user can continue chatting. The background task runs phases sequentially, writing to the `deliberation.phase_outputs` JSONB. This is the key architectural invariant: background deliberation does NOT write to the session's episode history.

### Dual delivery mechanism

1. **Immediate**: On completion, publish a `MessageReceived(channel="internal")` to the bus. If the engine is running and the session lock is available, the engine picks it up and the LLM formulates a response incorporating the deliberation result.
2. **Deferred**: If immediate delivery fails (engine stopped, bus error), the deliberation stays in `completed` + `delivered=false` state. On the next user message, context assembly calls `deliver_pending()` which finds these results and injects them into the system prompt.

### Phase output keying

When storing phase outputs, the JSONB key matches the *producing* phase (e.g., frame's output → `phase_outputs["frame"]`). The `update_phase` store function was extended with an optional `output_key` parameter to support this, while maintaining backward compatibility.

### Tool dispatch via lazy import

The `start_deliberation` tool is dispatched from `memory/tools.py`, but the deliberation engine imports `TOOL_DEFINITIONS` from the same module. To avoid circular imports, the tool handler uses a lazy import (`from theo.conversation.deliberation import start_deliberation` inside the function body). The `execute_tool` function was extended with an optional `session_id` parameter that flows through `stream_and_collect` to support session-scoped tools.

## Files changed

- `src/theo/conversation/stream.py` — **new**: shared `stream_and_collect()` helper
- `src/theo/conversation/deliberation.py` — **new**: deliberation engine with phase state machine
- `src/theo/conversation/turn.py` — refactored to use `stream_and_collect()`
- `src/theo/memory/tools.py` — added `start_deliberation` tool handler, `session_id` param
- `src/theo/memory/_schemas.py` — added `start_deliberation` tool schema
- `src/theo/conversation/context/assembly.py` — inject pending deliberation results
- `src/theo/conversation/context/formatting.py` — `deliberation_section` param in `join_system_prompt()`
- `src/theo/config.py` — deliberation settings (max_phases, phase_timeout, budget_tokens)
- `src/theo/errors.py` — `DeliberationError`
- `src/theo/deliberation.py` — `output_key` param on `update_phase()`
- `tests/test_deliberation_engine.py` — **new**: 25 tests covering full lifecycle
- `tests/test_stream.py` — **new**: 8 tests for shared stream helper
- `tests/test_memory_tools.py` — updated for new tool, added deliberation tool tests
- `tests/test_conversation.py` — updated patches for refactored imports
- `tests/test_resilience.py` — updated patches for refactored imports
- `tests/test_onboarding.py` — updated tool count, added deliver_pending mock
- `tests/test_user_model.py` — updated tool count
- `tests/test_context.py` — added deliver_pending mock fixture
