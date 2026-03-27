# Structured user model

**Date:** 2026-03-27
**Ticket:** FPL-21

## Context

The `core_memory` table has a freeform `user_model` JSONB slot where Claude can write anything. For M2 ("Theo remembers"), we need structured tracking of user dimensions across established psychological frameworks — with confidence scores that grow over time as evidence accumulates. The freeform JSONB slot remains as a scratchpad; the structured table becomes the system of record.

## Decisions

### Dedicated `user_model_dimension` table over extending core_memory

A separate table with one row per framework/dimension pair gives us typed constraints (confidence range, evidence count positivity), individual update timestamps, and queryability by framework. The alternative — deepening the existing `user_model` JSONB slot — would make confidence tracking ad-hoc and querying expensive.

### Confidence ramp: `min(1.0, evidence_count / 10.0)`

A simple linear ramp where 10 evidence points = full confidence. This is deliberately naive — good enough for M2, and easily replaceable with Bayesian updating in later milestones without schema changes. The computation happens in SQL (`LEAST(1.0, (evidence_count + 1)::real / 10.0)`) so it's atomic with the evidence increment.

### Seven canonical frameworks seeded at migration time

Schwartz values, Big Five, narrative identity, communication preferences, energy patterns, goals, and boundaries. All 29 dimensions are seeded with `confidence=0.0` and empty values so the onboarding conversation (FPL-29) can populate them. The `ON CONFLICT DO NOTHING` guard makes the migration idempotent.

### Single LLM tool for updates

One `update_user_model` tool with a framework enum rather than separate tools per framework. This keeps the tool list small (Claude's tool-use performance degrades with too many tools) while the enum provides schema validation. Read access is not exposed as a tool — context assembly (FPL-28) will inject relevant dimensions into the system prompt.

### Module follows `core.py` pattern exactly

SQL constants at module level, `_row_to_result` helper, public async functions with OTEL spans, `DimensionResult` frozen dataclass in `_types.py`. This consistency makes the codebase predictable and reviewable.

## Files changed

- `src/theo/db/migrations/0008_user_model.sql` — table DDL, trigger, index, seed data
- `src/theo/memory/_types.py` — `DimensionResult` dataclass
- `src/theo/memory/user_model.py` — `read_dimensions`, `get_dimension`, `update_dimension`
- `src/theo/memory/tools.py` — `update_user_model` tool definition and dispatcher
- `src/theo/memory/__init__.py` — export `DimensionResult`
- `tests/test_user_model.py` — CRUD, confidence ramp, tool schema, tool execution tests
