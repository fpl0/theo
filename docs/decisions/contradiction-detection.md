# Contradiction Detection (FPL-26)

## Context

As the knowledge graph grows, conflicting facts are inevitable ("Alice works at Google" vs "Alice works at Meta"). Without detection, Theo silently accumulates contradictory information, degrading the quality of memory retrieval and reasoning. M2 adds contradiction detection at storage time so conflicting nodes have reduced confidence and are linked for future resolution.

## Decision: Fire-and-forget post-processing in store_node

Contradiction detection runs as a background task (`asyncio.create_task`) after `store_node()` completes. The store operation always succeeds immediately — the contradiction check never blocks or fails the caller. Errors in the background task are logged and swallowed. This keeps the write path fast while still catching conflicts opportunistically.

## Decision: LLM-based contradiction judgement at reactive speed

Semantic similarity alone cannot distinguish contradictions from related-but-compatible statements. After filtering candidates above a 0.7 similarity threshold, each candidate is sent to the LLM (reactive tier / Haiku) with a structured JSON prompt. This is cheap and fast enough for fire-and-forget use, and the LLM can reason about semantic nuance that vector distance cannot capture.

## Decision: Confidence reduction with database-level floor

Both the new node and the conflicting node have their confidence reduced by 0.3. The SQL uses `GREATEST(confidence - $2, 0.1)` to floor confidence at 0.1 atomically, ensuring nodes are never fully discredited — they remain available for future manual resolution. The reduction and floor values are constants rather than config to keep the initial implementation simple.

## Decision: Deferred import to avoid circular dependency

`nodes.py` imports `contradictions.py` inside `_run_contradiction_check()` rather than at module level. Since `contradictions.py` imports `search_nodes` from `nodes.py`, a top-level import would create a circular dependency. The deferred import resolves this cleanly.

## Decision: "contradicts" edge linking conflicting nodes

`resolve_contradiction` creates an edge with `label="contradicts"` and `weight=1.0` between the two nodes, with the LLM's explanation in `meta`. This makes contradictions visible in graph traversal and queryable via `get_edges(node_id, label="contradicts")`, enabling future UI or automated resolution workflows.

## Decision: Separate transactions for confidence and edge (accepted tradeoff)

Confidence reduction runs in its own transaction, followed by edge creation via `store_edge` in a separate transaction. If `store_edge` fails after confidences have been reduced, nodes will have lower confidence without a linking `contradicts` edge. This is an accepted tradeoff: combining them into a single transaction would require duplicating the expire-then-insert logic from `store_edge`, coupling the two modules. The confidence reduction is still correct (a contradiction was detected), and the missing edge only affects discoverability, not data correctness. The fire-and-forget context means partial failures are logged and tolerated.

## Files changed

- `src/theo/config.py` — added `contradiction_check_enabled: bool` to Settings
- `src/theo/memory/contradictions.py` — new module with `check_contradiction`, `resolve_contradiction`, `ConflictResult`
- `src/theo/memory/nodes.py` — fire-and-forget integration in `store_node()`, `drain_background_tasks()` for shutdown
- `src/theo/__main__.py` — calls `drain_background_tasks()` before closing the database pool
- `tests/test_contradictions.py` — unit tests for detection, resolution, config toggle, edge cases
- `docs/decisions/contradiction-detection.md` — this file
