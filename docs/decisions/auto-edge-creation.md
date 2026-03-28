# Auto-edge Creation from Entity Co-occurrence (FPL-27)

**Date:** 2026-03-27

## Context

The knowledge graph has nodes and edges but no automatic relationship discovery. When Theo stores memories about related concepts in the same conversation, they should be linked. The `episode_node` join table (migration 0003) exists for cross-referencing episodes to entities but was unused. FPL-20 provides `store_edge()` for edge upsert.

## Decision: Co-occurrence via episode_node join table

Rather than using embedding similarity to discover relationships (expensive and imprecise), we use the `episode_node` table as a co-occurrence signal. When `store_memory` creates a node, it records which episode triggered the creation via `record_mention`. At the end of each turn, `extract_and_link` queries for node pairs that co-occur in the same episodes within the session and creates `co_occurs` edges.

The SQL self-join (`en1.node_id < en2.node_id`) ensures each pair is counted once and avoids directional duplication.

## Decision: Weight formula `min(1.0, co_count * 0.2)`

Linear scaling capped at 1.0 with a 0.2 step per co-occurrence. Five or more co-occurrences saturate the weight. This is deliberately simple — a more sophisticated formula (e.g., TF-IDF style) would be premature given the current scale. The weight is re-computed on each call via `store_edge`'s expire-then-insert pattern, so it naturally strengthens over time.

## Decision: Fire-and-forget post-turn extraction

`extract_and_link` runs as `asyncio.create_task` after the assistant episode is stored. Errors are caught and logged, never propagated to the user. This keeps the critical path (response delivery) fast while still creating edges opportunistically.

## Decision: Explicit `link_memories` LLM tool

In addition to automatic co-occurrence edges, Claude can create explicit relationship edges via the `link_memories` tool. These use `weight=0.8` (high confidence since Claude explicitly chose to link them) and carry `meta.source = "llm_tool"` for provenance. This complements the automatic `co_occurs` edges with semantically richer, labeled relationships.

## Decision: `episode_id` threaded through `execute_tool`

The optional `episode_id` parameter on `execute_tool` passes the current user episode's ID down to `_store_memory`, which calls `record_mention`. This avoids global state or context variables while keeping the change minimal — other tools simply ignore the parameter.

## Files changed

- `src/theo/memory/auto_edges.py` — new module: `record_mention`, `extract_and_link`
- `src/theo/memory/tools.py` — new `link_memories` tool, `episode_id` parameter on `execute_tool`, `record_mention` integration in `_store_memory`
- `src/theo/conversation/turn.py` — capture `user_episode_id`, pass to `execute_tool`, fire-and-forget `extract_and_link`
- `tests/test_auto_edges.py` — full test coverage for new module and integration
- `docs/decisions/auto-edge-creation.md` — this file
