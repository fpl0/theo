# Autonomy Classification (FPL-33)

**Date:** 2026-03-28

## Context

M3 introduces Theo's initiative — deliberation, intents, plans. Every action needs a trust layer determining how much owner involvement is required. Without it, Theo either needs approval for everything (useless) or acts autonomously on everything (dangerous). The autonomy classifier sits between intent and execution.

## Decisions

### Four autonomy levels, not binary

Four levels (`autonomous`, `inform`, `propose`, `consult`) give graduated control. Binary (do/don't) lacks nuance — "inform" is the sweet spot for routine operations where the owner wants awareness without friction. The levels map directly to UX patterns: autonomous = silent, inform = notification after, propose = inline keyboard approval, consult = ask before even planning.

### Static registry over LLM classification

Action types map to default levels via a Python dict, not an LLM call. LLM classification adds latency, cost, and non-determinism to every tool call. The action type space is small and well-defined — a lookup table is faster, cheaper, and predictable. Owner overrides provide the escape hatch for customization. LLM-based classification can be added later for novel action types if needed.

### Owner overrides in core memory, not a separate table

Overrides live in the `context` core memory document under `autonomy_overrides`. Core memory is already loaded in every context assembly — no extra query. The owner can modify overrides through natural conversation ("always ask before updating my goals") via the existing `update_core_memory` tool. A separate `autonomy_overrides` table would add query complexity for a dataset that fits in a single JSON object.

### Tool-to-action-type mapping for turn integration

Tool names (e.g., `store_memory`) map to action types (e.g., `memory_store`) via a static dict. This decouples the classification system from tool naming — action types are semantic (what's happening), tool names are implementation (how it's invoked). Unknown tools default to `external_action` (propose) — safe by default.

### Action log as flat table, not event sourcing

Every classified action gets a row in `action_log` with the classification result and decision outcome. This is simpler than publishing events and reconstructing state — the audit trail is a single table scan. The `intent_id` FK links to background intents when applicable. This table is the foundation for M5's autonomy graduation (counting consecutive successes per action type).

### No integration with intent evaluator yet

FPL-32 (intent queue and evaluator) is on a separate branch, not yet merged. The autonomy module provides all the building blocks — `classify()`, `log_action()`, `requires_approval()` — that the intent evaluator will call. Integration will happen when both are on the same branch.

### Turn-level integration via tool classification

In `turn.py`, each tool call is classified before execution. Autonomous and inform levels execute immediately (inform will notify via bus event when the approval gateway exists). Propose and consult levels currently execute but log the action — the approval gateway (FPL-37) will gate execution for these levels. This approach lets us ship the classification infrastructure without blocking on the approval UI.

## Files changed

- `src/theo/autonomy.py` — new module: classification, action logging, owner overrides
- `src/theo/db/migrations/0011_action_log.sql` — action_log table with indexes
- `src/theo/conversation/turn.py` — tool calls classified and logged
- `src/theo/errors.py` — (no changes needed, using standard exceptions)
- `tests/test_autonomy.py` — unit tests for all classification and logging paths
- `tests/test_conversation.py` — updated for autonomy integration
- `docs/decisions/autonomy-classification.md` — this document
