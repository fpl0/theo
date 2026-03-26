# Node Operations (FPL-8)

## Decision: Result types as frozen dataclasses

Shared result types (`NodeResult`, `EpisodeResult`) use `@dataclasses.dataclass(frozen=True, slots=True)` rather than Pydantic models or NamedTuples. Rationale:

- **Frozen** prevents accidental mutation of query results.
- **Slots** reduces memory overhead — these are high-volume objects.
- **No Pydantic** because result types are internal read-only projections, not validated input. Pydantic's validation overhead would be wasted.
- **Not NamedTuple** because dataclasses support default values cleanly (`similarity: float | None = None`).

## Decision: Domain types as `type` aliases over `Literal`

`TrustTier` and `SensitivityLevel` are defined as `type` statement aliases wrapping `Literal` unions, matching the PostgreSQL domain types from migration 0001. This gives static type checking at call sites without runtime overhead.

## Decision: `int` return from `store_node`, not UUID

The ticket mentions "Returns UUID" but the `node` table uses `bigint GENERATED ALWAYS AS IDENTITY`. The implementation returns `int` to match the actual schema. No UUID translation is needed.

## Decision: Separate SQL for filtered vs unfiltered search

Two SQL constants (`_SEARCH_NODES` and `_SEARCH_NODES_BY_KIND`) rather than dynamic query building. This keeps SQL static and avoids string concatenation, which is easier to audit and cache at the database level.

## Decision: MLX stub in conftest for cross-platform testing

Added a `MagicMock`-based stub for `mlx` in `tests/conftest.py` that activates only when `mlx` is not importable (non-Apple-Silicon environments). This allows the full test suite to run on Linux CI without requiring Apple hardware.
