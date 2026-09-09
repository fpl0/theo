# ADR-0001 requirement matrix

Updated 9 September 2026. This matrix maps the original ADR to the current source layout and available evidence. A deterministic test, a local native evaluation and production qualification establish different scopes. See [acceptance status](acceptance.md) for remaining gates and [the evidence index](evidence/README.md) for dated reports.

## Binding goals

| ID | Goal | Implementation/evidence | Status |
|---|---|---|---|
| G01 | New implementation | `storage.py`, `config.py`, `cli/`; clean-root CLI test | Implemented; no Luke runtime imports |
| G02 | Included-subscription inference | backends/policy.py; A11–A14 | Fail-closed paths tested; real billing hard-stop evidence pending |
| G03 | Four backends/model portability | `backends/claude.py`, `backends/codex.py`, `backends/acp.py`; protocol fixtures; native smoke/complex reports | Four implemented; local Claude/Codex inference and handoff passed; full live qualification pending; automatic failover absent |
| G04 | SQLite memory authority | `memory/`, `storage.py`; A01–A08/A34/A35 | Core invariants tested; warm-vector target check pending |
| G05 | Durable continuity | `work/jobs.py`, `application/coordinator.py`, `memory/context.py`; A09/A10/A15–A17 | Canonical state and local native handoff tested; unlimited semantic compaction absent |
| G06 | Personal behaviour | Persona seed, canonical voice instructions; complex evaluation; fixed 30-case pack | Complex native transcripts reviewed; separate production 30-case gate pending |
| G07 | Tools/media/research/coding | 51 broker tools; `channels/`, `content/`, `execution/` | Original 33-tool contract plus expanded media tests; partial live Telegram evidence; video sampled, native vision coverage pending |
| G08 | Predictable autonomy | `work/autonomy.py`, `work/scheduling.py`, `work/improvement.py`, `delivery/` | Eleven loops and policy tested; background activation gated |
| G09 | Durable delegation | `work/jobs.py`, `application/coordinator.py`; A17/A18/A27; complex evaluation | Durable outcomes/final obligations tested; local native child jobs produced verified artifacts |
| G10 | Controlled actions | `tools/`, `delivery/`, `execution/`, `privacy.py`; A19/A20/A23/A27–A31 | Fencing, approvals, uncertainty and local Mac boundary tested; full target deployment pending |
| G11 | Evidence-based improvement | `work/improvement.py`, `execution/workspaces.py`, `operations/` | Skills and proposals tested; unattended self-patch orchestration incomplete |
| G12 | Operability | `cli/`, `operations/`, `supervisor.py`, `observability/`; A32–A35/A39/A40 | Local operations, supervisor and telemetry tested; target service/install/rollback/soak pending |
| G13 | Data portability | `operations/importer.py`, `operations/export.py`; A02/A35/A36 | Synthetic snapshot import/export tested; private-data parity unavailable |
| G14 | Honest completion | Acceptance status, dated evidence, `operations/qualification.py` | Evidence and remaining limits recorded; production qualification incomplete |

## Mandatory acceptance scenarios

Test paths below are relative to `tests/`; additional source paths are under `src/theo/`.

| ID | Scenario | Evidence | Status / limitation |
|---|---|---|---|
| A01 | Empty root | test_memory.py, test_scheduling_operations.py | Pass |
| A02 | Delete exports | test_memory.py | Pass |
| A03 | Concurrent CAS | test_memory.py | Pass |
| A04 | Ambiguous model correction | test_memory.py | Pass |
| A05 | Exact reviewed correction | test_memory.py | Pass |
| A06 | Archive/index race | test_memory.py | Pass |
| A07 | Embedding outage | test_memory.py | Pass for outage; live assets pending |
| A08 | Dense/repeated context | test_memory.py; complex evaluation | Context budget and local native reassessment/voice cases pass; production behavior pack separate |
| A09 | Tool evidence across handoff/compaction | test_memory.py, test_protocols.py; complex evaluation | Canonical fixtures and local Claude/Codex handoff pass; arbitrarily long semantic compaction absent |
| A10 | Older native session | test_memory.py | Fresh canonical-session strategy tested |
| A11 | Unknown telemetry/error terminal | test_protocols.py | Pass |
| A12 | Paid/config contamination | test_protocols.py | Pass |
| A13 | Exhausted shared pools | test_protocols.py, test_tools_runtime.py | Waiting persistence pass; no auto-route failover |
| A14 | Critic/reflection eligibility | test_protocols.py; shared Coordinator | Shared gate pass; live inference pending |
| A15 | Cancel native/tool execution | test_delivery_jobs.py, test_protocols.py, test_macos_isolation.py | Fencing, owned process-group cleanup and local Mac boundaries pass; Telegram Stop during active inference pending |
| A16 | Inbound commit then crash | test_crash_durability.py, test_delivery_jobs.py | Real process-kill durability pass |
| A17 | Durable child restart | test_tools_runtime.py, test_delivery_jobs.py, test_supervisor.py; complex evaluation | Persistence, local daemon recovery and native child final obligations pass; target service qualification separate |
| A18 | Child empty/failure/report | test_tools_runtime.py | Pass |
| A19 | Remote-acceptance uncertainty | test_delivery_jobs.py, test_recovery_regressions.py; Telegram recovery report | Injected timeout and real Telegram lost-acknowledgement reconciliation pass |
| A20 | Partial multipart send | test_delivery_jobs.py, test_recovery_regressions.py | Chunk receipts, cancellation, reconciliation and restored uncertainty pass |
| A21 | Two-week outage | test_scheduling_operations.py | Simulated deterministic timing pass; no soak claim |
| A22 | DST gap/fold | test_scheduling_operations.py | Pass: gap skip, fold earlier once |
| A23 | Correction invalidates queued draft | test_delivery_jobs.py | Pass |
| A24 | Attention/blackout | test_delivery_jobs.py | Interactive cap separation tested; real engagement qualification pending |
| A25 | Goals require steps/evidence | test_scheduling_operations.py | Pass |
| A26 | Eleven autonomy loops | test_scheduling_operations.py | Typed no-op cases and grounded work pass |
| A27 | Worktree promotion/reminder fairness | test_operational_boundaries.py | Real Git worktrees and stale-head rejection pass |
| A28 | Stale generation | test_delivery_jobs.py, test_tools_runtime.py | Pass at transaction boundary |
| A29 | Cross-task quality attribution | test_tools_runtime.py | Pass with explicit run IDs |
| A30 | Untrusted input and scope | test_tools_runtime.py, test_telegram_integration.py; `content/web.py`, `privacy.py`; complex evaluation | Grant/path/group-privacy checks and local native malicious-source case pass; wider adversarial/live group coverage pending |
| A31 | Actual OS/control denial | test_operational_boundaries.py, test_macos_isolation.py | Local macOS sandbox checks pass; Linux root-only canary skips on Mac; full target control-path gate pending |
| A32 | Self-patch/release rollback | test_operational_boundaries.py | Integrity/schema gate pass; automatic self-patch rollback incomplete |
| A33 | Supervisor recovery/pause/alert | test_supervisor.py; observability evidence | Local supervisor recovery/pause and test-bot alert checks pass; target service and total-machine outage coverage separate |
| A34 | Full/busy/broken SQLite | test_operational_boundaries.py, test_memory.py | Pass against actual SQLite |
| A35 | Backup during writes/restore | test_scheduling_operations.py; benchmark.py | Pass for local synthetic data and quarantine |
| A36 | Repeat partial Luke import | test_scheduling_operations.py | Synthetic history/tombstone/quarantine pass; unavailable schemas unqualified |
| A37 | All 33 baseline tool schemas | test_tools_runtime.py, test_architecture.py; tool-schemas.json | Baseline contracts pass; published schemas match all 51 registered tools |
| A38 | Caption/HTML/buttons/media | test_telegram_integration.py, test_delivery_jobs.py, test_operational_boundaries.py; Telegram client reports | Rich rendering, chunks, callbacks and expanded media tested; partial live client evidence; remaining media/playback/model checks pending |
| A39 | Runtime version change | test_protocols.py | Fingerprint gate pass; real vendor-update canary pending |
| A40 | Fresh operator workflow | test_scheduling_operations.py; operations.md | CLI init/doctor pass; full Mac service/release walkthrough pending |

## Baseline tools

All 51 registered tools, their original baseline membership and exact generated schemas are in [tools.md](tools.md).

## Later implementation coverage

| Addition | Current source and checks | Remaining scope |
| --- | --- | --- |
| Telegram destinations, edits, albums, reviews and recovery | `channels/telegram/`, `privacy.py`, migrations 003; test_telegram_integration.py, test_telegram_setup.py, test_recovery_regressions.py | See [client validation](telegram-implementation.md) for live group/media/model gaps. |
| Interactive terminal | `channels/terminal/`; test_terminal.py | Named sessions, attachments, previews and local final delivery covered; native qualification remains separate. |
| Observability | `observability/`, migration 004; test_telemetry.py; [local runbook and evidence](observability.md) | Bounded local load and correlation verified; successful live Codex usage telemetry and external outage monitoring pending. |
| Source organization and packaging | test_architecture.py; `scripts/check_installed_package.py` | Import direction, schemas and installed entry points checked; historical build reports retain their original source paths. |
