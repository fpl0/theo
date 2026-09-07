# ADR-0001 requirement matrix

This matrix deliberately distinguishes host mechanisms from native or production evidence. A passing unit/fixture check is not a claim that the whole corresponding production goal is satisfied.

## Binding goals

| ID | Goal | Implementation/evidence | Status |
|---|---|---|---|
| G01 | New implementation | storage/config/package; clean-root CLI test | Implemented; no Luke runtime imports |
| G02 | Included-subscription inference | backends/policy.py; A11–A14 | Fail-closed paths tested; real billing hard-stop evidence pending |
| G03 | Four backends/model portability | backends/native.py; protocol subprocess fixtures | Four implemented; all live qualifications pending; automatic failover absent |
| G04 | SQLite memory authority | memory/context/embeddings/storage; A01–A08/A34/A35 | Core invariants tested; warm-vector target check pending |
| G05 | Durable continuity | jobs/runtime/context; A09/A10/A15–A17 | Canonical state tested; actual provider compaction/handoff pending |
| G06 | Personal behaviour | persona seed; context voice/reassessment; 30-case pack | Mechanisms implemented; live behavioural grades pending |
| G07 | Tools/media/research/coding | 44 broker tools, channels/artifacts/browse/media/workspaces | 33 baseline contract covered; live rich-media/vision pending; video first-frame only |
| G08 | Predictable autonomy | autonomy/scheduling/improvement/delivery; A21–A26 | Eleven loops + policy tested; background activation gated |
| G09 | Durable delegation | jobs/runtime; A17/A18/A27 | Durable outcomes and final obligations tested |
| G10 | Controlled actions | tools/delivery/isolation/workspaces; A19/A20/A23/A27–A31 | Fencing, approvals and uncertainty tested; real OS gate blocked |
| G11 | Evidence-based improvement | improvement/workspaces/operations | Skills and proposals tested; unattended self-patch orchestration incomplete |
| G12 | Operability | CLI/operations/supervisor; A32–A35/A39/A40 | Core operations tested; target service/install/rollback/soak pending |
| G13 | Data portability | importer/operations; A02/A35/A36 | Synthetic snapshot import/export tested; private-data parity unavailable |
| G14 | Honest completion | acceptance/compatibility/qualification | Tested vs blocked/partial recorded; production qualification false |

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
| A08 | Dense/repeated context | test_memory.py; behaviour pack | Budget pass; live voice grading pending |
| A09 | Tool evidence across handoff/compaction | test_memory.py, test_protocols.py | Canonical fixture pass; actual native compaction pending |
| A10 | Older native session | test_memory.py | Fresh canonical-session strategy tested |
| A11 | Unknown telemetry/error terminal | test_protocols.py | Pass |
| A12 | Paid/config contamination | test_protocols.py | Pass |
| A13 | Exhausted shared pools | test_protocols.py, test_tools_runtime.py | Waiting persistence pass; no auto-route failover |
| A14 | Critic/reflection eligibility | test_protocols.py; shared Coordinator | Shared gate pass; live inference pending |
| A15 | Cancel native/tool execution | test_delivery_jobs.py; native process lifecycle | State/fencing tested; target process/credential denial pending |
| A16 | Inbound commit then crash | test_crash_durability.py, test_delivery_jobs.py | Real process-kill durability pass |
| A17 | Durable child restart | test_tools_runtime.py, test_delivery_jobs.py | Persistence/final obligations pass; daemon integration blocked |
| A18 | Child empty/failure/report | test_tools_runtime.py | Pass |
| A19 | Remote-acceptance uncertainty | test_delivery_jobs.py | Injected ambiguous timeout/reconciliation pass |
| A20 | Partial multipart send | test_delivery_jobs.py | Pass |
| A21 | Two-week outage | test_scheduling_operations.py | Simulated deterministic timing pass; no soak claim |
| A22 | DST gap/fold | test_scheduling_operations.py | Pass: gap skip, fold earlier once |
| A23 | Correction invalidates queued draft | test_delivery_jobs.py | Pass |
| A24 | Attention/blackout | test_delivery_jobs.py | Interactive cap separation tested; real engagement qualification pending |
| A25 | Goals require steps/evidence | test_scheduling_operations.py | Pass |
| A26 | Eleven autonomy loops | test_scheduling_operations.py | Typed no-op cases and grounded work pass |
| A27 | Worktree promotion/reminder fairness | test_operational_boundaries.py | Real Git worktrees and stale-head rejection pass |
| A28 | Stale generation | test_delivery_jobs.py, test_tools_runtime.py | Pass at transaction boundary |
| A29 | Cross-task quality attribution | test_tools_runtime.py | Pass with explicit run IDs |
| A30 | Untrusted input and scope | test_tools_runtime.py; browse.py | Host grant/path checks pass; adversarial live model pending |
| A31 | Actual OS/control denial | test_operational_boundaries.py | Skipped: host forbids UID transition; Mac control-path gate pending |
| A32 | Self-patch/release rollback | test_operational_boundaries.py | Integrity/schema gate pass; automatic self-patch rollback incomplete |
| A33 | Supervisor recovery/pause/alert | test_supervisor.py | Skipped: host forbids Unix server sockets; target needed |
| A34 | Full/busy/broken SQLite | test_operational_boundaries.py, test_memory.py | Pass against actual SQLite |
| A35 | Backup during writes/restore | test_scheduling_operations.py; benchmark.py | Pass for local synthetic data and quarantine |
| A36 | Repeat partial Luke import | test_scheduling_operations.py | Synthetic history/tombstone/quarantine pass; unavailable schemas unqualified |
| A37 | All 33 tool schemas | test_tools_runtime.py; tool-schemas.json | Pass; optional arguments retained |
| A38 | Caption/HTML/buttons/media | test_delivery_jobs.py, test_operational_boundaries.py | Plain-text chunk/API model tests pass; styled HTML intentionally omitted; live media pending |
| A39 | Runtime version change | test_protocols.py | Fingerprint gate pass; real vendor-update canary pending |
| A40 | Fresh operator workflow | test_scheduling_operations.py; operations.md | CLI init/doctor pass; full Mac service/release walkthrough pending |

## Baseline tools

All 33 names, the additional handlers and exact generated schemas are in [tools.md](tools.md).
