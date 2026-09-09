# Complex native evaluation — 9 September 2026

This evaluation runs real Codex and Claude subscription CLIs through Theo's coordinator, MCP broker, SQLite state and local delivery receipts. It evaluates visible answers separately from successful process exits. Both final batches passed: **40 native turns, four host-state checks and 398 automated assertions**. All 40 visible answers also passed the separate five-dimension review, with a minimum score of 4/5 and no critical violations. The passing results apply to the selected models and reviewed source snapshot.

## Results

| Batch | Automated cases | Transcript acceptance | Evidence |
| --- | ---: | --- | --- |
| Codex-led | 22/22 | 20/20, minimum 4/5 | [Raw](evidence/complex-codex.json), [review](evidence/complex-codex-review.json), [scored](evidence/complex-codex-scored.json) |
| Claude-led | 22/22 | 20/20, minimum 4/5 | [Raw](evidence/complex-claude.json), [review](evidence/complex-claude-review.json), [scored](evidence/complex-claude-scored.json) |

Codex was usually more concise. Claude gave fuller explanations and often repeated details that the owner did not need. Both respected the explicit listening boundary, took no unsolicited memory/task actions in those cases, and changed their explanation after negative feedback. Both preserved uncertainty before recommending a resend. Empathy remained somewhat formulaic in places; this is a documented style limitation, not a claim of perfect personality.

Codex recovered from one rejected schedule input without a duplicate commit. It also attempted to read an unavailable global goal skill; the broker denied the path and the model continued through Theo's permitted goal tool. Those recovered frictions receive lower judgment scores. Final Claude used Theo's memory, scheduling and delegation tools directly. The earlier false historical-error claim disappeared in the final handoff JSON.

The subjective mean scores below summarize this small sample. They are not calibrated model rankings. Aggregating by actual backend gives the same means here as the led batches because both peer handoff answers received 5 in every dimension.

| Actual backend | Correctness | Evidence | Completion | Voice | Judgment |
| --- | ---: | ---: | ---: | ---: | ---: |
| Codex gpt-5.6-sol | 4.85 | 4.95 | 4.95 | 4.55 | 4.85 |
| Claude Opus 5 | 4.80 | 4.80 | 5.00 | 4.25 | 4.90 |

## Scope and reproducibility

The selected models are Codex `gpt-5.6-sol` (CLI `0.153.4`) and Claude `claude-opus-5` (Claude Code `2.1.265`). Claude's native initialization metadata confirms Opus 5. Each complete batch has 20 native turns and two host-state checks. A batch runs 19 turns on its primary backend and one on the other backend to test handoff; two complete batches therefore exercise 20 turns on each actual backend.

Tests use random synthetic project data, temporary data roots, restricted tool grants and a local delivery sink. Reminder checks advance Theo's application clock and verify actual due-time admission, deduplication and receipts while models are paused. These runs do not cover Telegram delivery, deployment isolation, quota exhaustion, multi-day operation or all possible conversations.

At evaluation time, the working branch was `main`, matching freshly fetched `origin/main` at `2664e1433746cb716b96b5f17bcc2bd45346690a`. During final testing, the separate “Assess Telegram bot integration” task began changing the same checkout. Final qualification therefore uses a frozen build of this review's changes, reconstructed from its already-built wheel plus the final worker-instruction correction. The [build manifest](evidence/complex-reviewed-build.json) records source/test hashes, wheel hash and validation. Concurrent Telegram changes were neither reverted nor included in this qualification. The isolated wheel was installed separately and checked byte-for-byte against its frozen sources, including both migrations.

## Cases

| Area | Native turns per batch | What must be observed |
| --- | ---: | --- |
| Memory | 5 | One durable save; correction stays pending until synthetic owner approval; current revision survives recall, archive and restoration; historical revision remains recoverable. |
| Reasoning | 3 | Correct optimal schedule; primary-source measurement distinguished from an unresolved conflicting claim; imported instructions ignored; no invented birthday or spreadsheet delivery. |
| Autonomy | 4 | Goal persisted without premature execution; invoice artifact written, read, registered and used to complete the step and goal; a real child job produces a separately verified artifact. |
| Scheduling | 2 | Fifteen-minute and two-hour reminders saved with exact due times and timezone; recurring reminder cancelled and its history retained. |
| Personality and explanation | 4 | Brief empathy; listening without advice, questions or unsolicited effects; accurate uncertainty explanation; a clearer everyday example after negative feedback. |
| Model handoff | 2 | Other backend uses the actual completed invoice artifact; original backend preserves that evidence and reads an intervening owner fact revision. |

The two additional host checks verify paused/unpaused autonomy admission and reminder delivery while models are paused. Every native turn also requires a canonical context, one terminal outcome, a completed job and exactly one successful attempt per final delivery chunk.

## Bugs and evaluation corrections

The [code review](review-2026-09-08.md) records fifteen concrete bugs and their regressions. Complex live runs specifically exposed missing application time, Codex native delegation bypassing Theo's durable queue, Claude Markdown memory bypassing Theo's searchable memory, insufficient schedule receipts and orphaned process cleanup. Those failures led to production fixes, not exceptions in the acceptance checks.

Answer review also caught invented currency units, unnecessary internal details, unsafe advice to retry an unresolved send, unsupported reassurance, and an incorrect assertion that a previously supported answer had no evidence after a fact changed. Theo's trusted persona and voice now reach native instruction channels separately from user and recalled content. Worker instructions require exact-action evidence before recommending a retry, preserve supplied units, distinguish changed facts from historical errors, and honor requested JSON keys.

Some failures were evaluator mistakes: automatic canonical memory retrieval is valid retrieval; a complete JSON code block is a valid human-facing JSON answer; and task letters `ABC`, `A,B,C` and an array represent the same task order when the prompt specifies no field type. Regression tests cover these equivalences and reject surrounding prose, wrong revisions, wrong orders and extra tasks. The listening prompts explicitly prohibit memory/task creation so that the requested reply-only boundary is unambiguous. Handoff checks were strengthened to reject unsolicited JSON fields. Durable-state requirements were not relaxed.

Earlier failed runs are retained in `docs/evidence/complex-*-initial.json`, `complex-*-first-full.json`, `complex-*-pre-final-review.json` and the intermediate reports. Sonnet 4.5 passed the smaller smoke suite but did not meet the complex answer-quality bar; the selected final Claude model is Opus 5. An interrupted mixed-snapshot run is also retained and is not counted as a completed evaluation.

## Quality review method

This task's Codex coding agent reviews every visible final answer together with recorded tool calls, current canonical sources and state assertions. This is an unblinded subjective review by the same agent making the fixes, not an independent human panel or a statistical estimate of model reliability. The cases were used during iteration, so final success is regression evidence rather than a held-out benchmark.

Each transcript receives scores from 1 to 5 for correctness, evidence, completion, voice and judgment. A 4 means good with a minor flaw; a 5 means especially precise and well suited to the request. Acceptance requires all automated checks, all five scores at least 4 on every transcript, and zero critical violations. The offline scorer rejects missing reviews and cannot override failed state checks. Raw reports retain `quality_review: null`; separate scored reports bind the review to the raw report's SHA-256.

## Offline and CI checks

The frozen reviewed build passed **156 offline tests**, with one Linux root-only boundary canary skipped on macOS. Ruff lint/format and strict Pyright pass; wheel and source distribution build successfully. Native scripts require explicit `--live` and refuse CI, GitHub Actions and offline-test environments before launching a model. Sixteen offline refusal tests cover all four native/behavior entry points. This evaluation did not change the GitHub workflow. Later checkout and CI changes are described in [current quality checks](code-quality.md).

See [local reproduction commands](live-testing.md#complex-behavior-and-answer-quality).
