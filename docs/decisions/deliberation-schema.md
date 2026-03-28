# Deliberation Schema and Working Memory (FPL-30)

**Date:** 2026-03-28

## Context

Deliberative reasoning (FPL-36) needs persistent state for multi-step sessions that survive turns, sessions, and restarts. This is the data layer — schema, migration, CRUD operations. The deliberation engine itself is a separate concern (FPL-36).

## Decisions

### Flat JSONB for phase outputs, not a separate steps table

Each deliberation row has a `phase_outputs` JSONB column that accumulates results as keys (`{frame: "...", gather: "...", ...}`). Five phases = five keys max. This avoids join complexity — the entire deliberation state is readable in a single row. A separate `deliberation_step` table would give relational purity but adds query complexity for the primary access pattern (load full state for context assembly or resumption). The JSONB approach also simplifies the `update_phase` operation to a single atomic `||` merge.

### Top-level module, not inside conversation/

The ticket specifies `conversation/deliberation_store.py`, but `conversation` is currently a single file (`conversation.py`) with 40+ test patches targeting `theo.conversation.*`. Converting it to a package would break all those patches and is better done as part of FPL-36 (which introduces the deliberation engine and explicitly restructures conversation into a package). For now, `theo.deliberation` lives at the top level — same pattern as other Theo modules. FPL-36 can relocate it when it restructures the conversation module.

### Deliberation is a conversation concern, not memory

`DeliberationState` lives in `theo.deliberation`, not `theo.memory._types`. Deliberation represents *how* Theo thinks (process state), not *what* Theo remembers (knowledge). Keeping it separate from the memory module avoids coupling the reasoning lifecycle to the retrieval/storage layer.

### Standard exceptions over custom TheoError subclasses

`LookupError` for missing/non-matching deliberations and no custom exceptions. The operations are straightforward CRUD — `update_phase` and `complete_deliberation` raise `LookupError` when the WHERE clause matches no running row, which is the correct semantic (the thing you're looking for doesn't exist in the expected state). Custom exceptions can be added if downstream consumers need finer-grained catch blocks.

### Partial indexes for hot paths

Two partial indexes target the evaluator and delivery hot paths:
- `WHERE status = 'running'` — evaluator finds active deliberations without scanning completed ones
- `WHERE status = 'completed' AND NOT delivered` — delivery scanner finds pending results

Both are narrow by design — most deliberations will be completed+delivered, so these indexes stay small.

### updated_at trigger for consistency

Added `updated_at` column with the shared `_set_updated_at()` trigger, following the database convention. Phase updates and status changes automatically get timestamped, which aids debugging and ordering.

## Files changed

- `src/theo/db/migrations/0010_deliberation.sql` — new migration
- `src/theo/deliberation.py` — new module with `DeliberationState` dataclass and CRUD operations
- `tests/test_deliberation.py` — tests for all operations and edge cases
- `docs/decisions/deliberation-schema.md` — this document
