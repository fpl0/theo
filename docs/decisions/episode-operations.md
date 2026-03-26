# Episode Operations (FPL-9)

## Decision: Mirror node operations patterns

Episode operations follow the same structural patterns as node operations: static SQL constants, singleton `db`/`embedder` imports, `_row_to_result` helper, OTEL spans on every public function. Consistency across the memory package reduces cognitive load and makes the codebase predictable.

## Decision: `int` return from `store_episode`, not UUID

The ticket mentions "Returns UUID" but the `episode` table uses `bigint GENERATED ALWAYS AS IDENTITY`, matching the node table. The implementation returns `int` to match the actual schema.

## Decision: Separate SQL for session-filtered vs unfiltered search

Two SQL constants (`_SEARCH_EPISODES` and `_SEARCH_EPISODES_BY_SESSION`) rather than dynamic query building — same rationale as node operations. Static SQL is easier to audit, and the query planner can cache plans per statement.

## Decision: Default limit of 50 for list, 10 for search

`list_episodes` defaults to 50 (a reasonable conversation window), while `search_episodes` defaults to 10 (matching `search_nodes`). List retrieves a known session's history; search is a cross-session similarity query where fewer, higher-quality results are preferred.

## Decision: Chronological ordering for list, similarity ordering for search

`list_episodes` orders by `created_at ASC` (oldest first) since it reconstructs a conversation timeline. `search_episodes` orders by cosine distance (closest first) since it retrieves semantically relevant episodes. These match the natural access patterns for each operation.
