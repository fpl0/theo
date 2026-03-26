# Core Memory Operations (FPL-10)

## Decision: Dedicated result types in core.py, not _types.py

`CoreDocument` and `ChangelogEntry` live in `theo.memory.core` rather than the shared `_types.py` module. Core memory has a distinct shape (label/body/version) unrelated to nodes or episodes, and keeping types co-located with their data access layer makes the module self-contained. They're re-exported from `theo.memory.__init__` for external consumers.

## Decision: Label validation via `Literal` type alias + runtime check

`CoreMemoryLabel` is a `Literal["persona", "goals", "user_model", "context"]` type alias for static checking. A runtime `_validate_label()` function checks against a `frozenset` mirror because label values arrive as plain strings from tool calls. This dual approach gives both compile-time and runtime safety.

## Decision: Single-transaction update with explicit connection

`update()` uses `async with db.pool.acquire() as conn, conn.transaction()` to run the SELECT (read old body), UPDATE, and INSERT (changelog) on one connection in one transaction. This guarantees atomicity without relying on implicit transaction behaviour of individual pool calls.

## Decision: `read_one` alongside `read_all`

Although `read_all` is the primary path (context assembly loads all 4 documents), `read_one` supports targeted reads during tool execution (e.g., "show me my current goals"). Both raise on invalid labels; `read_one` additionally raises `LookupError` if the row is missing (which shouldn't happen with seeded data, but is defensive).

## Decision: No custom exceptions for core memory errors

`ValueError` for invalid labels and `LookupError` for missing documents are standard Python exceptions appropriate here. Core memory operations are simple enough that a custom exception hierarchy would add abstraction without value. If error handling needs evolve, these can be wrapped later.
