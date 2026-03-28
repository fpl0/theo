# Merge Pipeline — 2026-03-28

## PRs Merged (in order)

### 1. PR #27 — Speed Classification + Session Ratchet (FPL-31)
- **Commit**: `834c18f` (squash)
- **Checks**: All passed (568 tests)
- **Conflicts**: None
- **Fixes applied**: None needed
- **Unblocks**: Foundation for context-aware speed routing

### 2. PR #28 — Deliberative Reasoning Engine (FPL-36)
- **Commit**: `967b30b` (squash)
- **Checks**: All passed (604 tests after fixes)
- **Conflicts**: Clean auto-merge with PR #27 changes
- **Fixes applied post-merge**:
  - Removed unused `uuid4` import in `test_deliberation_engine.py` (ruff F401)
  - Updated 2 ratchet test patches from `theo.conversation.turn.stream_response` → `theo.conversation.stream.stream_response` (PR #28 extracted stream helper to `stream.py`)
- **Unblocks**: FPL-38 (metacognitive monitor)

### 3. PR #29 — Autonomy Classification (FPL-33)
- **Commit**: `132bfd9` (squash)
- **Checks**: All passed (642 tests after fixes)
- **Conflicts**: `turn.py` — PR #28 extracted stream/tool loop to `stream.py`, PR #29 had autonomy classification in the old inline `_execute_tools`
- **Resolution**: Moved autonomy imports and classification logic into `stream.py`'s `_run_tools` function. Updated test fixture patches from `theo.conversation.turn` → `theo.conversation.stream`.
- **Migration**: `0011_action_log.sql` — clean, next sequential
- **Unblocks**: FPL-37 (approval gateway), FPL-39 (project plans)

## PRs Skipped

### PR #30 — Budget Controls (FPL-34)
- **Reason**: No review comment yet
- **Migration**: Claims `0011_token_usage.sql` — now conflicts with merged `0011_action_log.sql`. Must renumber to `0012` before merge.

### PR #26 — Intent Queue & Evaluator (FPL-32)
- **Reason**: Review says "blocked on conversation module rebase (item 4)"
- **Blockers**:
  1. Targets stale `conversation.py` (now `conversation/engine.py` package)
  2. Migration was `0010_intent.sql` — must now renumber to `0012` (0010 = deliberation, 0011 = action_log)
  3. Needs rebase onto current main

## Current State of Main

- **Test count**: 642 passing
- **Latest migration**: `0011_action_log.sql`
- **All linters**: green (ruff, ruff format, sqlfluff, ty)

## Linear Tickets Updated

- FPL-31: Done ✅
- FPL-36: Done ✅
- FPL-33: Done ✅
