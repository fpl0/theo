# Privacy Filter Pipeline (FPL-25)

**Date:** 2026-03-28

## Context

Trust tiers (`owner` through `untrusted`) and sensitivity levels (`normal`, `sensitive`, `private`) are defined as PostgreSQL domain types and enforced at the database level. However, no application-level enforcement existed — any trust tier could store data at any sensitivity. M2 adds a three-stage pipeline that runs at the storage boundary in `store_node()` and `store_episode()`.

## Decision: Pure function with `get_settings()` for config

`evaluate()` is a synchronous, pure function (aside from reading config and emitting a span). It calls `get_settings()` to check `privacy_filter_enabled` rather than accepting a settings parameter, keeping the call site minimal. Tests patch `get_settings` to inject disabled/enabled state. This matches the existing pattern where modules call `get_settings()` directly.

## Decision: Three-stage pipeline in a single function

Rather than splitting stages into separate public functions or a class hierarchy, all three stages (trust check, content classification, sensitivity assignment) run inside a single `evaluate()` call. The stages are internal helpers (`_check_trust`, `_classify_content`, `_assign_sensitivity`). This keeps the public API surface minimal — callers only see `evaluate()` and `escalate_sensitivity()`.

## Decision: Keyword/regex heuristics for content classification

Stage 2 uses compiled regex patterns per category rather than ML-based classification. This is intentional for M2 — it's fast, deterministic, and has zero dependencies. The patterns cover five categories (financial, medical, identity, location, relationship) with conservative matching. ML-based classification can replace this in a future milestone without changing the public API.

## Decision: Sensitivity escalation, never downgrade

When the privacy filter recommends a higher sensitivity than what the caller passed, the final sensitivity is escalated. The reverse never happens — if a caller explicitly marks data as `private`, the filter cannot downgrade it to `normal`. This is enforced via `escalate_sensitivity()` which takes the `max()` of both ordinals. The same helper is exported for use by integration points.

## Decision: Rejection raises `PrivacyViolationError`

When `evaluate()` returns `allowed=False`, the storage functions (`store_node`, `store_episode`) raise `PrivacyViolationError` rather than silently dropping the data. This makes violations visible to callers (the tool execution layer in `tools.py` catches all exceptions and returns error strings to Claude). Silent drops would hide policy violations.

## Decision: Trust tier caps sensitivity at storage boundary

External and untrusted sources are capped at `normal` sensitivity. Verified and inferred sources are capped at `sensitive`. Only owner/owner_confirmed can store at `private`. When content classification requires a sensitivity level higher than what the trust tier allows (e.g., financial content from an untrusted source needs `sensitive`, but untrusted caps at `normal`), the operation is rejected rather than silently capping.

## Files changed

- `src/theo/memory/privacy.py` — new module with `evaluate()`, `escalate_sensitivity()`, `PrivacyDecision`
- `src/theo/memory/nodes.py` — integrated privacy filter into `store_node()`
- `src/theo/memory/episodes.py` — integrated privacy filter into `store_episode()`
- `src/theo/errors.py` — added `PrivacyViolationError`
- `src/theo/config.py` — added `privacy_filter_enabled` setting
- `tests/test_privacy.py` — comprehensive tests for all stages, tiers, and integration
- `docs/decisions/privacy-filter.md` — this file
