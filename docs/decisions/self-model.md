# Self Model Initialization (FPL-22)

**Date:** 2026-03-27

## Context

M2 requires Theo to begin tracking its own accuracy per domain so future milestones (M5) can build calibration curves and scoring. This ticket provides the schema, seed data, and basic record/read operations.

## Decision: Result type in `_types.py`, not in `self_model.py`

Unlike `CoreDocument` which lives in `core.py`, `DomainResult` goes in the shared `_types.py` module. The self-model result type has a straightforward shape (id, domain, accuracy counters) similar to `NodeResult` and `EpisodeResult`. Placing it in `_types.py` keeps the pattern consistent and makes it available to future modules (e.g., calibration scoring in M5) without circular imports.

## Decision: Atomic accuracy recomputation in SQL

`record_outcome` computes accuracy directly in the UPDATE statement using `(correct_predictions + delta) / (total_predictions + 1)` rather than reading, computing in Python, and writing back. This avoids race conditions if multiple outcomes are recorded concurrently and eliminates a round-trip. The RETURNING clause provides the updated row in a single query.

## Decision: `ValueError` for unknown domains

Following the core memory pattern, unknown domain names raise `ValueError` rather than a custom exception. Domains are seeded in the migration and grow slowly (new migration to add a domain), so an unknown domain is a programming error, not a user-facing condition.

## Decision: No custom exceptions

The self-model module is simple enough that standard Python exceptions (`ValueError`) cover all error cases. If domain management becomes more complex in M5, custom exceptions can be introduced then.

## Files changed

- `src/theo/db/migrations/0009_self_model.sql` — table, trigger, seed data
- `src/theo/memory/_types.py` — `DomainResult` dataclass
- `src/theo/memory/self_model.py` — `read_domains()`, `record_outcome()`
- `src/theo/memory/__init__.py` — export `DomainResult`
- `tests/test_self_model.py` — unit tests
