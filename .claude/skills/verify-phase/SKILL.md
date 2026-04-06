---
name: verify-phase
description: Verify that a specific phase of the Theo implementation plan is complete. Checks file existence, runs tests, and validates every Definition of Done item.
argument-hint: <phase number, e.g. 1, 2, 3, 7.5>
user-invocable: true
---

# Verify Phase Completion

Check whether a specific phase of the Theo implementation plan is
complete by validating every Definition of Done item.

## Phase-to-File Mapping

| Phase | Plan File                                                     |
| ----- | ------------------------------------------------------------- |
| 1     | `docs/plans/foundation/01-foundation.md`                      |
| 2     | `docs/plans/foundation/02-event-types.md`                     |
| 3     | `docs/plans/foundation/03-event-log-and-bus.md`               |
| 4     | `docs/plans/foundation/04-memory-schema.md`                   |
| 5     | `docs/plans/foundation/05-embeddings-and-knowledge-graph.md`  |
| 6     | `docs/plans/foundation/06-episodic-and-core-memory.md`        |
| 7     | `docs/plans/foundation/07-hybrid-retrieval.md`                |
| 7.5   | `docs/plans/foundation/00a-bootstrap-identity.md`             |
| 8     | `docs/plans/foundation/08-models-and-privacy.md`              |
| 9     | `docs/plans/foundation/09-mcp-memory-tools.md`                |
| 10    | `docs/plans/foundation/10-agent-runtime.md`                   |
| 11    | `docs/plans/foundation/11-cli-gate.md`                        |
| 12    | `docs/plans/foundation/12-scheduler.md`                       |
| 13    | `docs/plans/foundation/13-background-intelligence.md`         |
| 14    | `docs/plans/foundation/14-subagents-onboarding-lifecycle.md`  |

## Steps

1. **Read the plan file** for the requested phase. Extract:
   - "Scope > Files to create" — the file list
   - "Definition of Done" — the checklist
   - "Test Cases" — the test specifications

2. **Check file existence** — for every file in the scope, verify it exists with Glob.

3. **Run the phase's tests** — execute `bun test <test-file>` for each
   test file listed in the phase. Report pass/fail per file.

4. **Run regression tests** — execute `bun test` (all tests) to ensure
   this phase didn't break prior phases.

5. **Run the quality gate** — execute `just check` (biome + tsc + tests). All three must pass.

6. **Verify each DoD item** — go through the "Definition of Done" checklist one by one:
   - For code items: grep for the function, type, or pattern described
   - For behavior items: check the test output confirms the behavior
   - For migration items: verify the SQL file exists and contains the expected DDL

7. **Report results** in this format:

```text
Phase N: [Name] — [COMPLETE | INCOMPLETE]

Files: M/N exist
Tests: M/N pass
Quality gate: [pass | fail]
Regression: [pass | fail]

Definition of Done:
  [x] Item 1 — verified by [method]
  [x] Item 2 — verified by [method]
  [ ] Item 3 — MISSING: [what's wrong]

Remaining work:
  - [specific items still needed, if any]
```

## Rules

- Do not implement anything. This skill is verification only.
- Be precise: "test X fails with error Y" is useful. "Some tests fail" is not.
- If a dependency phase is not complete, say so: "Phase N depends on Phase M, which is incomplete."
- Always run regression tests. A phase is not complete if it breaks earlier phases.
