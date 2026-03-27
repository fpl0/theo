# Edge Operations (FPL-20)

## Context

The `edge` table exists since migration 0002 with temporal validity (`valid_from`/`valid_to`), weight constraints, and a unique partial index ensuring one active edge per (source, target, label) triple. No application code existed to operate on edges.

## Decision: Upsert via expire-then-insert in a single transaction

Rather than `INSERT ... ON CONFLICT UPDATE`, `store_edge` explicitly expires any active edge with the same key, then inserts a new row. This preserves the full history of edge changes (old rows remain with `valid_to` set) and works naturally with the partial unique index on `(source_id, target_id, label) WHERE valid_to IS NULL`. Both operations run inside `async with conn.transaction()` for atomicity, matching the pattern in `core.update`.

## Decision: Separate SQL constants per direction and filter combination

`get_edges` uses four SQL constants covering the cross product of direction (outgoing/incoming) and label filter (with/without). This avoids dynamic SQL construction, keeps queries static for plan caching, and is easy to audit. The `both` direction simply unions the outgoing and incoming results in Python.

## Decision: Recursive CTE for graph traversal

`traverse` uses a `WITH RECURSIVE` CTE to walk outgoing edges up to `max_depth` hops. Cycle prevention uses `e.target_id <> ALL(g.path)` to avoid revisiting nodes already in the path. `DISTINCT ON (node_id)` with `ORDER BY cumulative_weight DESC` picks the best path per node when multiple paths exist, and final ordering is done in Python for clarity.

## Decision: `tuple[int, ...]` for `TraversalResult.path`

`TraversalResult` is a frozen dataclass, but `frozen=True` only prevents attribute reassignment — it does not prevent mutation of mutable containers. Using `tuple[int, ...]` instead of `list[int]` ensures the path is truly immutable, matching the frozen contract. The `_row_to_traversal` helper converts the PostgreSQL array to a tuple at the boundary.

## Decision: `execute` return string for expire

`expire_edge` uses `db.pool.execute()` which returns a command tag string like `"UPDATE 1"`. Comparing against `"UPDATE 1"` is simpler than using `fetchval` with `RETURNING` and handles the not-found case naturally.

## Files changed

- `src/theo/memory/_types.py` — added `EdgeResult` and `TraversalResult` frozen dataclasses
- `src/theo/memory/edges.py` — new module with `store_edge`, `get_edges`, `traverse`, `expire_edge`
- `src/theo/memory/__init__.py` — exported `EdgeResult` and `TraversalResult`
- `tests/test_edges.py` — unit tests covering all operations
- `docs/decisions/edge-operations.md` — this file
