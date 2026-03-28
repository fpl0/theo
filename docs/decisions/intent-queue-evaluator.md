# Intent queue and evaluator

**Date:** 2026-03-28
**Ticket:** FPL-32
**Status:** Accepted

## Context

Theo needs to evolve from purely reactive (respond to user messages) to
initiative-taking. The intent queue lets any module declare "something I want to
do", and the evaluator processes these intents in priority order within a token
budget. This is the proactive backbone for M3.

## Decisions

### Separate from the event bus

The event bus handles immutable facts about things that already happened. Intents
represent things Theo *wants* to do — they have priority, budget estimates,
lifecycle state, and can be deferred or cancelled. Mixing the two would
conflate their semantics and complicate both systems.

### Dynamic priority model

```
priority = base_priority + recency_boost + deadline_urgency
```

Computed at fetch time in SQL, never stored. This avoids stale priority values
and lets intents naturally bubble up as deadlines approach. Recency boost gives
up to +10 for intents created within the last hour. Deadline urgency gives up to
+30 as an intent's deadline approaches.

### Single-intent processing with FOR UPDATE SKIP LOCKED

The evaluator processes one intent at a time. `FOR UPDATE SKIP LOCKED` provides
future-proof concurrency if multiple evaluator instances run, though the current
design uses a single background task.

### Three-tier throttle

The evaluator checks `engine.inflight` to avoid competing with active
conversations:
- **Active** (inflight > 0): skip the cycle entirely
- **Idle** (inflight == 0, recent message): process normally
- **Absent** (>15 min since last message): higher throughput possible

### Migration numbered 0008

The ticket specified migration 0011, but the current codebase only has
migrations through 0007. Used 0008 as the next sequential number. FPL-30's
migration (0010 in the ticket) hasn't been merged yet; when it lands the
numbers will sort correctly regardless of order.

### Built-in intent sources

Two sources implemented for M3 launch:
1. **Date scanner** — regex scan of `core_memory.context` for approaching dates
2. **Contradiction detected** — placeholder handler registered; the actual
   wiring into `contradictions.py` is deferred until that module exists

The date scanner is a standalone function (`scan_approaching_dates`) callable
from the evaluator loop or externally.

## Files changed

- `src/theo/db/migrations/0008_intent.sql` — intent table with indexes
- `src/theo/errors.py` — `IntentExpiredError`, `IntentBudgetExhaustedError`
- `src/theo/config.py` — evaluator settings
- `src/theo/intent/__init__.py` — module exports, `publish_intent` wrapper
- `src/theo/intent/_types.py` — `IntentResult` frozen dataclass
- `src/theo/intent/store.py` — CRUD operations with OTEL spans
- `src/theo/intent/evaluator.py` — `IntentEvaluator` class, date scanner
- `src/theo/conversation.py` — evaluator inflight/message notifications
- `src/theo/__main__.py` — evaluator lifecycle integration
- `tests/test_intent.py` — comprehensive test suite
