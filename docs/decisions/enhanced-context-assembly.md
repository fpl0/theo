# Enhanced context assembly

## Context

M1's context assembly used flat budgets (`memory_budget=2000`, `history_budget=4000`) and pure vector search (`search_nodes`). Core memory was included as a single block without per-section token accounting. M2 requires hybrid retrieval, per-section budget enforcement, and a defined eviction policy to handle budget pressure gracefully.

## Decisions

### Hybrid search replaces vector-only retrieval

Replaced `search_nodes()` from `theo.memory.nodes` with `hybrid_search()` from `theo.memory.retrieval`. The hybrid search fuses vector similarity, full-text search, and graph traversal via Reciprocal Rank Fusion (RRF). This gives more diverse and contextually relevant results without changing the retrieval API shape (`list[NodeResult]`).

### Per-section token budgets in Settings

Added two new config fields: `context_user_model_budget` and `context_current_task_budget`. These join the existing `context_memory_budget` and `context_history_budget`. All are env-configurable via `THEO_` prefix. Budgets removed from `assemble()` function signature — they are now config-driven, simplifying the call site in `turn.py`. Persona and goals have no budget fields because they are never truncated — adding unused config would be misleading. A validator ensures all budget fields are positive.

### Tiered eviction policy

Budget enforcement happens at three levels:

1. **User model and current task** are unconditionally capped at their configured budgets (`context_user_model_budget`, `context_current_task_budget`). This prevents a growing user model from crowding out other sections.
2. **Retrieved memories** are capped at `context_memory_budget` — the most expendable since they are re-queried every turn.
3. **History** is capped at `context_history_budget` — oldest messages dropped first (existing behavior preserved).
4. **Persona and goals are never truncated** — these define Theo's identity and purpose; losing them would compromise response quality. No budget fields exist for them.

This matches the MemGPT priority hierarchy: identity > goals > user model > retrieved context > history.

### SectionTokens dataclass for observability

Added `SectionTokens` frozen dataclass with per-section token counts. `AssembledContext` now includes this as a field. OTEL span attributes updated to emit `context.persona_tokens`, `context.goals_tokens`, `context.user_model_tokens`, `context.task_tokens`, `context.memory_tokens`, `context.history_tokens`. This replaces the single `context.core_tokens` attribute from M1.

## Files changed

- `src/theo/config.py` — two new per-section budget fields (`context_user_model_budget`, `context_current_task_budget`), budget validator
- `src/theo/conversation/context.py` — hybrid search, per-section formatting, eviction policy, `SectionTokens` dataclass, updated OTEL attributes
- `tests/test_context.py` — updated for `hybrid_search`, config-driven budgets, new tests for eviction, section ordering, protected sections, and `SectionTokens`
- `tests/test_conversation.py` — updated `AssembledContext` construction with `section_tokens`
- `tests/test_resilience.py` — updated `AssembledContext` construction with `section_tokens`
- `docs/decisions/enhanced-context-assembly.md` — this file
