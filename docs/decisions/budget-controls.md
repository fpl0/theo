# Budget Controls and Token Tracking (FPL-34)

**Date:** 2026-03-28

## Context

With M3's deliberative reasoning and background intent execution, Theo's token consumption could spiral unchecked. Budget controls track usage per LLM call and enforce configurable daily and session caps, providing both visibility and guardrails.

## Decisions

### Top-level budget module, not inside conversation/

`theo.budget` is a standalone module because budget enforcement applies beyond conversation turns — deliberation phases and intent execution will also need budget checks. Keeping it at the top level avoids a dependency from other modules back into conversation.

### Record-per-call, not aggregated counters

Each LLM call inserts a row into `token_usage` with full context (session, model, source, cost). This gives maximum query flexibility for dashboards and debugging. The cost of extra rows is negligible vs. the value of per-call granularity. Aggregate queries use simple `SUM` with time-range filters.

### Cost estimation via per-tier rates, not per-model

Cost weights are per speed tier (reactive/reflective/deliberative) rather than per model ID. Model IDs change frequently with Anthropic releases; tiers are stable abstractions. When pricing changes, updating three numbers in config is simpler than maintaining a model-to-cost lookup table.

### Warning via bus event, hard cap via exception

Warning threshold (default 80%) emits a `BudgetWarning` event on the bus. The Telegram gate (or any future gate) can subscribe and notify the owner. Hard caps raise `BudgetExceededError`, which turn execution catches and converts to a graceful user-facing message. No retry queuing for budget exhaustion — it's not a transient failure.

### Budget check before LLM call, recording after

`check_budget()` runs at the start of `execute_turn()`, before context assembly or streaming. This fails fast and avoids wasted work. `record_usage()` runs inside `_stream_one_iteration()` after each `StreamDone`, so multi-iteration tool loops accumulate correctly.

### No updated_at column on token_usage

Rows are write-once, never modified. No trigger needed.

## Files changed

- `src/theo/db/migrations/0011_token_usage.sql` — table + indexes
- `src/theo/config.py` — budget settings and validation
- `src/theo/errors.py` — `BudgetExceededError`
- `src/theo/bus/events.py` — `BudgetWarning` event
- `src/theo/budget.py` — new module: tracking, checking, cost estimation, OTEL metrics
- `src/theo/conversation/turn.py` — integration: check before call, record after stream
- `tests/test_budget.py` — full coverage of budget module
- `tests/test_conversation.py` — mock budget in existing turn tests
- `docs/decisions/budget-controls.md` — this file
