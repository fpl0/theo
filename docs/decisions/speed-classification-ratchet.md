# Speed Classification and Session Ratchet

**Date:** 2026-03-28
**Ticket:** FPL-31

## Context

`classify_speed()` was a pure function using only message-level heuristics (length and keyword regex). For M3's initiative-taking features, classification needs session awareness: once a conversation reaches deliberative depth, it should stay there to avoid jarring model switches mid-discussion.

## Decisions

### SessionContext over engine-internal state

Classification signals are packaged in a `SessionContext` dataclass passed to `classify_speed()` rather than having the function reach into engine internals. This keeps `classify_speed()` testable as a pure-ish function (the only impurity is reading `session_ratchet_enabled` from Settings). The engine builds the context and passes it in — classification logic stays in `llm.py`, session tracking stays in `engine.py`.

### Tuple return for observability

`classify_speed()` returns `(Speed, dict[str, object])` instead of just `Speed`. The signals dict captures every factor that contributed to the decision: base classification reason, ratchet state, history bias. These are set as span attributes (`speed.*`) on the `conversation.turn` span. This makes classification decisions fully debuggable in OpenObserve without adding log volume.

### Ratchet with explicit downgrade signals

The session ratchet holds the effective speed at the peak observed in the session. Downgrade is allowed only when the user sends a reactive message (greeting/ack) while the peak is deliberative — this pattern signals "got it, we're done with that topic." Other mid-session messages are ratcheted up. This is conservative: it's better to use a slightly overpowered model than to drop depth mid-conversation.

### Task indicator regex as separate signal

Added `_TASK_INDICATOR_RE` for planning/comparison language ("step by step", "pros and cons", "trade-offs") that implies deliberative intent without containing the original keyword set. This is a separate regex rather than extending `_DELIBERATIVE_RE` to keep signal attribution clear in the signals dict.

### History window cap

Session speed history is capped at 6 entries (`_HISTORY_WINDOW`). This prevents unbounded memory growth for long sessions while keeping enough context for meaningful history bias. The window is large enough to detect "mostly deliberative" conversations but small enough to be responsive to topic shifts.

### Speed cleanup with session locks

Session speed history is cleaned up alongside session locks when a session goes idle (lock released and not re-acquired). This ties speed state lifetime to session activity rather than requiring a separate TTL mechanism.

## Files changed

- `src/theo/llm.py` — `SessionContext`, updated `classify_speed()`, `_TASK_INDICATOR_RE`
- `src/theo/config.py` — `session_ratchet_enabled` setting
- `src/theo/conversation/engine.py` — `_session_speeds`, `session_context_for()`, `record_speed()`
- `src/theo/conversation/turn.py` — passes session context and signals to span
- `tests/test_llm.py` — `TestSessionRatchet`, updated `TestClassifySpeed` for tuple return
- `tests/test_conversation.py` — `TestEngineSessionContext`, ratchet integration tests
