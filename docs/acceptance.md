# Acceptance report — 7 September 2026

**Theo is implemented and tested locally. It is not production-qualified and has not been activated against the owner's accounts or Telegram.** The original ADR's final gate cannot be claimed from protocol fixtures, simulated clocks or configuration flags.

## Executed evidence

| Check | Observed result | Scope |
|---|---|---|
| Deterministic suite | 83 passed, 2 explicit host skips | Python 3.14.6, Linux x86_64; zero model-account calls |
| Static types | Pyright strict: zero errors/warnings | Application source |
| Formatting/lint | Ruff check and format | Application, scripts, tests |
| Native transports | Four actual adapters exercised against native-protocol subprocess fixtures | Claude streaming CLI; Codex App Server; Cursor/Grok official ACP SDK |
| Native installed schema | Codex CLI `0.151.0-alpha.2`: version/help and generated App Server schema inspected | No login, account inference or provider canary |
| SQLite failures | Busy/full/broken statement roll back; later writes remain usable | Real SQLite engine |
| Crash durability | Inbox commit survives an actual child-process kill; replay creates one logical job | Local isolated database |
| Concurrent work | Two isolated Git worktrees; changed promotion target rejected; due reminder bypasses full model slots | Real Git, synthetic jobs |
| Delivery | Partial chunk success, no-effect retry, uncertain timeout, review hashes and stale facts | Fault-injected sender plus real aiogram request model construction |
| Backups/restore | Online backup during writes, exact external blobs, tamper rejection, new-root quarantine | Isolated fixture data |
| Capacity | 20,000 memory records, 250,000 messages; 50 context runs | [Raw measurements](capacity-results.json) |
| Context latency | 156.29 ms p95; 108.34 ms median | FTS + graph, no warm vector service |
| Backup + verify / restore | 2.53 s / 1.24 s | Same local synthetic fixture; target Mac RTO remains unmeasured |
| Behaviour pack | 30 fixed synthetic cases, rubric and sequential resumable runner | Pack validation only; native responses/grades not collected |

Exact locked dependency and transport status is in [compatibility.json](compatibility.json). Test names and supporting code are mapped in [requirements.md](requirements.md). The test suite's network guard rejects Internet socket connections; native fixtures use subprocess stdio and no vendor accounts.

## Explicitly unverified or blocked

1. **Native account qualification:** Claude Code and Codex must pass real subscription authentication, hard-stop billing, tool relay, actual canonical handoff/compaction, cancellation, quota/auth and media canaries. Cursor/Grok require their real account entitlements and compatible model selectors. No account was supplied, enrolled or used.
2. **OS/service boundary:** this environment returned `Operation not permitted` for both user-ID transitions and Unix server-socket creation. The dedicated-UID canary and supervisor/core integration tests report skips. Mac sandbox, MCP socket reachability, credential/control denial, launchd restart, independent alerts and rollback must be exercised on the target machine. OS isolation remains disabled by default.
3. **Local assets:** the embedding-model download was stopped by automatic approval review because an unexpected telemetry endpoint was untrusted and its metadata disclosure was not authorized. The installer now sets Hugging Face's documented telemetry opt-outs; the blocked operation was not retried. Warm-vector, Apple Silicon speech and real browser/media checks remain incomplete.
4. **Behaviour:** the 30 cases are unscored. Production requires actual model inputs/outputs, at least 90% acceptable outcomes and zero unauthorized actions, fabricated completions or destructive memory changes. The fixture protocol replies are not behavioural evidence.
5. **Soak:** no genuine seven-day observation has elapsed. No 99.5% availability claim is made. The fixed-clock tests only establish deterministic timing semantics.
6. **Release and import parity:** release staging/schema rollback are tested, but Mac canary-regression recovery and full production installation remain unverified. Import tests cover the documented synthetic snapshot shape; unavailable private Luke data and unsupported schema variants remain explicit limitations.

## Remaining implementation scope

These are implementation limitations, not external qualification claims:

- No automatic cross-provider/model failover. Verified route selection and explicit waiting-job retries are available; there is no paid fallback.
- Canonical checkpoint extraction is bounded. Automatic semantic compaction of arbitrarily long mandatory history is not implemented; oversized mandatory context stops visibly.
- Reflection/skills and code worktree/release primitives are implemented. Unattended self-patch preparation-to-production orchestration and automatic regression rollback are not enabled. Promotion remains an operator action.
- Telegram uses literal plain text instead of styled HTML. The current video path exposes a first-frame preview, not full temporal coverage. Speech assets require local provisioning.
- The 33 baseline handlers are present, but live Telegram media delivery, native vision, file-format fidelity across all artifact types and end-to-end adversarial model behaviour remain to be demonstrated.

The implementation must retain these limitations until concrete work and evidence close them. `qualification status` cannot become passing solely from `qualified_backends` or `soak_completed` configuration fields. Background autonomy remains paused, and no existing assistant was replaced.
