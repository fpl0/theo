# Onboarding conversation (FPL-29)

**Date:** 2026-03-28

## Context

Theo needs a structured way to learn about its owner. Rather than accumulating observations passively over weeks, a dedicated onboarding conversation seeds the user model quickly and intentionally. The conversation is built on Motivational Interviewing techniques and maps responses to the structured `user_model_dimension` table (FPL-21).

## Decisions

### New `onboarding/` package, not inline in conversation

The onboarding logic (state machine, phase prompts) is its own package rather than living inside `conversation/` or `memory/`. Rationale: onboarding is a cross-cutting feature that touches memory (state persistence), conversation (context injection), gates (command handler), and tools (advance phase). A dedicated package keeps the dependency direction clean — other modules import from `onboarding`, not the other way around (except `flow.py` importing `core` for persistence).

### State in core_memory.context JSONB, not a new table

Onboarding state is transient — it exists only during the conversation and is removed on completion. Storing it in the existing `core_memory.context` JSONB avoids a migration for a lifecycle-only concern. The `onboarding` key holds the full state dict; an `onboarding_completed` boolean flag persists after completion to distinguish "never started" from "completed".

### Phase prompts as plain strings, not templates

Each phase has a hand-crafted system prompt augmentation stored as a Python string constant. No Jinja, no dynamic template rendering. The prompts are stable, version-controlled, and easy to iterate on. If personalisation is needed later, it can be added without changing the architecture.

### advance_onboarding as an LLM tool, not automatic

Claude decides when a phase is complete by calling the `advance_onboarding` tool. This keeps the human in the loop (Claude can ask "shall we move on?") and avoids brittle heuristics about conversation length or topic coverage. The tool returns the next phase name so Claude can adjust its approach.

### Completion returns None, not a sentinel state

`advance_phase()` returns `None` when the final phase is advanced past, rather than an `OnboardingState` with a special "completed" phase. This keeps the type simple and makes the completion path explicit in callers.

### Context assembly prepends onboarding before persona

When onboarding is active, the phase prompt is the first section in the system prompt, before persona/goals/user_model. This ensures Claude's behavior is primarily shaped by the onboarding instructions during the flow.

## Files changed

- `src/theo/onboarding/__init__.py` — package exports
- `src/theo/onboarding/flow.py` — state machine (start, advance, complete, resume)
- `src/theo/onboarding/prompts.py` — phase definitions and system prompt augmentations
- `src/theo/memory/tools.py` — added `advance_onboarding` tool definition and handler
- `src/theo/conversation/context.py` — onboarding prompt injection in `assemble()`
- `src/theo/gates/telegram.py` — `/onboard` command handler
- `tests/test_onboarding.py` — state machine, prompts, tool, and context integration tests
- `tests/test_memory_tools.py` — updated tool count assertion (5 → 6)
- `tests/test_user_model.py` — updated tool count assertion (5 → 6)
