# Acceptance status

Updated 9 September 2026 against the current working tree and the dated evidence linked below.

**Theo is implemented and tested locally, with successful native Codex/Claude evaluations and real Telegram client checks. It is not production-qualified.** A new data root starts with no verified accounts and background autonomy paused. Production activation requires the evidence accepted by `theo qualification status`; repository tests and configuration flags cannot substitute for those records.

## Verified scope

The 9 September documentation refresh ran the current checkout on macOS: **238 offline tests passed, 1 skipped in 66.50 seconds**. The skip is the Linux root-only dedicated-UID canary. Ruff lint/format and strict Pyright passed. The README initialization, diagnostics, memory and status commands also passed against a temporary root with no model calls or messages. These checks are separate from the historical native/model reports below.

| Area | Evidence | What it establishes |
| --- | --- | --- |
| Offline implementation | [Requirement matrix](requirements.md), `tests/`, [quality checks](code-quality.md) | Memory revisions, SQLite failures, crash recovery, job fencing, schedules, delivery uncertainty, privacy, native protocol contracts and operational boundaries have deterministic coverage. Host-specific skips must be read with each result. |
| Native smoke tests, 8 September | [Codex](evidence/native-e2e-codex.json), [Claude](evidence/native-e2e-claude.json), [review](review-2026-09-08.md) | Both adapters passed 4/4 real response, memory and document cases through the coordinator, MCP and local delivery. The harness uses the operator's subscription login and substitutes for deployment attestation/isolation. |
| Complex evaluation, 9 September | [Report and scored transcripts](complex-evaluation-2026-09-09.md), [frozen build](evidence/complex-reviewed-build.json) | Codex gpt-5.6-sol and Claude Opus 5 passed 40 native turns, four host-state checks and 398 automated assertions. All 40 transcripts passed a separate subjective agent review. This is a small regression sample on a frozen snapshot, excluding concurrent Telegram work. |
| Telegram client, 9 September | [Implementation status](telegram-implementation.md), [recovery evidence](evidence/telegram-client-2026-09-09-recovery.json) | Private-chat controls, reminders, edits, media, reviews, synthetic streaming/Stop and actual lost-acknowledgement reconciliation were exercised through Telegram. Transport acceptance does not establish playback or model understanding. |
| macOS boundary | `tests/test_macos_isolation.py`, [Telegram validation record](telegram-implementation.md) | Local sandbox canaries exercise protected/sibling paths, generated-code credential denial, SQLite and installed Codex startup without inference. These checks do not qualify the complete target service deployment. |
| Observability, 9 September | [Runbook](observability.md), [load report](evidence/observability-local-2026-09-09.json), [dashboard review](evidence/dashboard-design-2026-09-09.md) | Correlated telemetry, queries, alerts and a ten-minute load test: 8,680 synthetic operations, 1,818,072,848 bytes peak whole-stack footprint. This is local observability evidence, not a seven-day assistant soak. |

The [evidence index](evidence/README.md) distinguishes raw reports, failed attempts and source snapshots. Counts from the frozen model review, later Telegram work and the current checkout describe different builds; they must not be combined into a single test result.

## Remaining qualification

1. **Subscription and provider gates.** Local Claude/Codex inference has succeeded, but deployment qualification still requires current account evidence, billing hard-stop controls, shared tools, canonical handoff, active process cancellation, media and auth/quota canaries. The latest recorded Telegram setup was waiting for spending-control attestation. Cursor and Grok have protocol fixture coverage; live qualification remains pending. Historical model names and runtime versions are not a current entitlement catalogue.
2. **Complete model-backed Telegram coverage.** Run the [model-backed suite](live-testing.md#telegram--model--telegram), dedicated group/topic checks and the remaining media/playback, feedback and failure scenarios in the [Telegram status report](telegram-implementation.md). Private-chat checks do not establish group delivery or active native-process Stop behavior.
3. **Target deployment and recovery.** Local macOS canaries and supervisor tests are evidence for specific boundaries. The intended target still needs service installation/restart, credential and control-path denial, encrypted-storage verification, independent alerts, backup/restore and release rollback as an integrated operator workflow. Local Grafana cannot report a total machine/network outage; the external heartbeat service is not recorded as connected.
4. **Assets and capacity.** Local speech and video extraction were observed in the Telegram test setup. Warm-vector context performance and full browser/media fidelity remain unqualified. Reproduce capacity and restore measurements on the target with its actual local assets; earlier lexical-only measurements do not establish that objective.
5. **Production behavior gate.** The complex evaluation has reviewed real answers. The separate fixed 30-case production pack still requires attributable grades for all cases, at least 90% acceptable results and zero critical violations. Its qualification contract is distinct from the complex regression suite.
6. **Seven-day service observation.** No genuine seven-day production soak is recorded. Measure awake/service-enabled time, failures, queue delays, backups and recovery. Simulated clocks and the ten-minute telemetry load test cannot establish 99.5% availability.

## Implementation limits

- Route selection is explicit; there is no automatic cross-provider/model failover or paid fallback. Auth/quota waits need a verified route and operator retry.
- Canonical checkpoints are bounded extracts. Arbitrarily long mandatory history has no automatic semantic compactor; overflow stops with an explicit compaction requirement.
- Workspaces, reviewed skills, improvement proposals and release primitives exist. Unattended self-patch preparation, deployment and automatic regression rollback are incomplete; promotion remains an operator action.
- Telegram rich rendering supports a bounded subset with literal fallback after definite rejection. Video uses up to eight timestamped samples, with partial coverage disclosed. Speech requires local assets.
- Telegram bot identity changes need an explicit binding migration; group migrations require operator review. Private group boundaries are enforced in code and offline tests, with live group qualification pending.
- All 51 tool schemas are published, including the 33 original baseline tools. Their existence does not establish every file format, native vision model or adversarial live conversation.
- Luke import covers the documented snapshot shape and synthetic fixtures. Private source-data parity and uninspected schema variants remain unqualified.

## Historical baseline — 7 September 2026

The original acceptance run used Python 3.14.6 on Linux x86_64 and reported **83 passed, 2 host skips**. UID transitions and Unix socket creation were unavailable on that host. Later macOS results supersede those host-specific blockers for local checks; they do not rewrite the original evidence.

| Measurement | Original result | Scope |
| --- | --- | --- |
| Context retrieval | 156.29 ms p95; 108.34 ms median | 20,000 memories, 250,000 messages, 50 FTS/graph context runs without warm vectors. [Capacity report](capacity-results.json). |
| Backup and verify / restore | 2.53 s / 1.24 s | Same synthetic fixture; target Mac recovery objective remains separate. |
| Installed release | Init/doctor canary passed from commit `2b21039` | [Release report](release-verification.json); no service activation. |
| Dependency/runtime inspection | Recorded package versions and native protocol status | [7 September compatibility snapshot](compatibility.json). Use `uv.lock` for the current dependency graph. |

The initial embedding download was blocked during that run; the installer was updated to disable Hugging Face telemetry. That historical result is not a current asset inventory. Check the selected data root with `theo doctor --json` and use the [asset instructions](operations.md#local-assets-and-media).
