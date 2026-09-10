# Working on Theo

Theo is a self-hosted personal assistant with canonical SQLite memory, durable jobs,
and Telegram/terminal interfaces. The distribution is `theo-assistant`; the Python
package and CLI are `theo`. Production reasoning uses native Claude Code, Codex App
Server, Cursor ACP, or Grok ACP subscription runtimes. Included usage must be
verified; there is no metered model API fallback.

This guide describes how to change the repository. Start with the owning service
and its tests, preserve the boundaries below, and update the relevant user guide
when behavior changes. Current source, `pyproject.toml`, `uv.lock`, and
[CI](.github/workflows/ci.yml) establish the implementation and executable checks.
Dated reports in `docs/` are evidence for their recorded source snapshot, not proof
that today's checkout or a running installation is qualified.

## Get oriented and run locally

Check `git status --short` and the current branch before editing. Preserve unrelated
work; use an isolated checkout if concurrent changes prevent a reliable result.
Do not include another task's changes in your commit or validation claims.

The project targets **Python 3.14** (`>=3.14,<3.15`, selected by `.python-version`)
and uses **uv**, a `src/` layout, and Hatchling. Run commands from the repository
root unless a command says otherwise:

```sh
uv sync --locked --extra browser --extra embeddings
uv run --no-sync theo --help
```

Use `--no-sync` after the locked install so later commands retain installed extras.
The browser/embedding extras install Python dependencies; browser binaries and
model weights are separate opt-in asset downloads. The `speech` extra requires
Apple Silicon macOS. Core development and offline tests also run on Linux.

Use a disposable data root for manual development checks. The default macOS root
is `~/Library/Application Support/Theo`; it may contain a real assistant's state.
`--data-root` is a global flag and must precede the subcommand:

```sh
theo_dev_root=$(mktemp -d "${TMPDIR:-/tmp}/theo-dev.XXXXXX")
uv run --no-sync theo --data-root "$theo_dev_root" init --timezone Europe/Dublin
uv run --no-sync theo --data-root "$theo_dev_root" doctor --json
uv run --no-sync theo --data-root "$theo_dev_root" memory remember "Synthetic development note"
uv run --no-sync theo --data-root "$theo_dev_root" memory search "Synthetic"
```

Missing account, isolation, and qualification checks are expected on this root.
These commands do not require inference or a Telegram token. `theo serve` runs the
daemon; `theo chat` is a separate client and needs the daemon on the **same root**.
`theo chat "text"` queues work and returns JSON; omitting text opens the interactive
terminal. See [README.md](README.md), [terminal usage](docs/terminal.md), and
[native setup](docs/operations.md#installation-and-isolation) before live use.

## Find the owner of a change

Paths in this table start at `src/theo/`; grouped filenames share the first
package named in their row. Import concrete modules directly; package initializers
are intentionally small.

| Change | Start here | Primary tests under `tests/` |
| --- | --- | --- |
| Shared contracts, configuration, persistence | `domain.py`, `config.py`, `storage.py`, `migrations/` | `test_memory.py`, `test_operational_boundaries.py`, `test_resource_lifecycle.py` |
| Daemon startup, loops, job execution, host commands | `application/service.py`, `coordinator.py`, `commands.py`, `status.py` | `test_tools_runtime.py`, `test_recovery_regressions.py`, `test_supervisor.py` |
| Admission, leases, cancellation, reminders, goals, autonomy | `work/jobs.py`, `scheduling.py`, `goals.py`, `autonomy.py`, `improvement.py` | `test_delivery_jobs.py`, `test_scheduling_operations.py`, `test_crash_durability.py` |
| Memory revisions, retrieval, context, embeddings | `memory/store.py`, `context.py`, `embeddings.py` | `test_memory.py`, `test_complex_evaluation.py` |
| Model-facing tools and run authorization | `tools/schemas.py`, `registry.py`, `contracts.py`, `authorization.py`, `broker.py`, `tools/handlers/` | `test_tools_runtime.py`, `test_mcp_shim.py`, `test_architecture.py` |
| Native protocols, eligibility, process transport | `backends/base.py`, `policy.py`, `process.py`, `claude.py`, `codex.py`, `acp.py`, `factory.py` | `test_protocols.py`, `test_native_live_guards.py`, `test_resource_lifecycle.py` |
| Actions, approvals, receipts, multipart delivery | `delivery/ledger.py`, `contracts.py`, `chunking.py` | `test_delivery_jobs.py`, `test_recovery_regressions.py`, `test_operational_boundaries.py` |
| Telegram admission, media, controls, rendering | `channels/telegram/adapter.py`, `state.py`, `normalization.py`, `media.py`, `controls.py`, `rendering.py`, `sender.py` | `test_telegram_integration.py`, `test_telegram_setup.py`, `test_e2e_observer.py` |
| Terminal sessions, attachments, presentation | `channels/terminal/client.py`, `attachments.py`, `presentation.py`, `interface.py` | `test_terminal.py` |
| Artifacts, web reads, media extraction | `content/artifacts.py`, `web.py`, `media.py` | `test_tools_runtime.py`, `test_telegram_integration.py`, `test_terminal.py` |
| Isolation, owned processes, workspace promotion | `execution/isolation.py`, `processes.py`, `registry.py`, `workspaces.py`, `files.py` | `test_macos_isolation.py`, `test_operational_boundaries.py`, `test_resource_lifecycle.py` |
| Backups, restore, import/export, releases, qualification | `operations/backups.py`, `releases.py`, `importer.py`, `export.py`, `qualification.py`; root `supervisor.py` | `test_scheduling_operations.py`, `test_operational_boundaries.py`, `test_supervisor.py` |
| Operator CLI grammar, execution, diagnostics | `cli/parser.py`, `commands.py`, `diagnostics.py`, `credentials.py` | `test_architecture.py`, `test_scheduling_operations.py`, `test_terminal.py` |
| Telemetry and read-only observation | `observability/telemetry.py`, `observer.py`; root `observer.py` entry point | `test_telemetry.py` |

For additional context, use the [documentation index](docs/README.md),
[architecture](docs/architecture.md), [tool catalogue](docs/tools.md), and
[requirement matrix](docs/requirements.md). Older documents may use module names
from before the package reorganization; follow current imports and this map.

The normal execution path is:

```text
Telegram adapter / terminal client
  -> committed inbox + message + job (work/jobs.py)
  -> daemon claims a leased job (application/service.py)
  -> coordinator assembles canonical context and starts a native backend
  -> native worker -> run-scoped MCP shim -> authorized tool broker -> services
  -> action + outbox ledger -> channel sender -> persisted delivery receipt
```

Host commands have a separate path in `application/commands.py`. Due requested
reminders can become delivery obligations directly, without claiming a model slot.

## Architecture and invariants

### Dependency direction and code style

- `domain.py` has no Theo dependencies. Shared contracts, storage, memory, work,
  content, and delivery must not depend on application, CLI, channels, or tools.
  Compose them in the application layer; inject callbacks for upward interactions.
- Tool handlers call feature services through an immutable `ToolCall`. They must
  not import the broker/registry, application, CLI, or channels. Telegram's sender
  owns transport, not job state, the delivery ledger, or Telegram persistence.
- Keep CLI grammar importable without loading execution services or credentials.
  Preserve the `theo`, `python -m theo`, `theo.mcp_shim`, `theo.supervisor`, and
  `theo.observer` entry points when moving internal modules.
- `tests/test_architecture.py` checks dependency direction and cycles, including
  deferred imports. Moving an import inside a function is not a cycle fix.
- Follow Python 3.14 typing and existing strict Pydantic contracts (`extra="forbid"`,
  frozen models). Use `TheoError` subclasses and typed outcomes for expected
  failures. Keep optional/heavy dependencies lazy at their owning boundary.
- Ruff enforces imports and production module/package docstrings; the format
  target is 100 columns. Explain each module's responsibility and important
  boundaries. Pyright checks production source in strict mode.

### Persistence and concurrency

- SQLite is authoritative for full memory bodies/revisions, facts, persona, goals,
  jobs, messages, contexts, and receipts. Markdown exports and provider sessions
  are projections. Large original media use immutable content-addressed blobs;
  extracted text and metadata remain canonical database records.
- Use the async `Database` API. `Database.write()` executes a **synchronous,
  SQL-only** callback on the dedicated writer under `BEGIN IMMEDIATE`. Keep
  related checks and writes in that transaction. Never perform network calls,
  subprocesses, slow file work, or nested database API calls inside it.
- Compose atomic operations using existing connection-taking helpers such as
  `Jobs.insert()` and `Delivery.prepare_in()`. Do not split a check and its mutation
  into independent awaited calls. Reads use separate bounded connections; close
  connections explicitly and use `Path.as_uri()` for SQLite read-only URIs.
- Preserve WAL, `synchronous=FULL`, foreign keys, rollback on failure, revision
  comparisons, semantic identities, and fencing generations. Use `db.clock()` for
  application time so deadlines, expiry, reminders, and recovery are testable.
- Add a new numbered SQL migration for schema changes. **Never edit an applied
  migration:** startup checks its checksum. Check every affected positional SQL
  insert, backup/export/import path, and release schema gate. Verify both fresh
  initialization and upgrading existing state, including the installed wheel.

### Authority, memory, and execution

- Only the host creates `ToolContext` identities, generations, workspaces, and
  grants. Every broker mutation uses `BoundDatabase`, which rechecks the lease
  **inside** the write transaction. An initial authorization check is insufficient.
- Preserve owner and conversation scoping on reads and writes. Unlabelled resources
  are private. Group isolation applies to memories, facts, goals, pins, skills,
  artifacts, messages, and tool results; filtering recall alone is insufficient.
  Use `privacy.py` and tool authorization rather than implementing a second policy.
- Keep trusted persona/voice instructions separate from user and retrieved
  content in `ExecutionRequest.instructions`. Recheck selected memory revision,
  archive status, and scope when committing a context snapshot. Keep lexical
  retrieval available when embeddings are absent or unhealthy.
- Every native attempt starts with fresh canonical context. Provider auto-memory,
  native subagents, or provider automations cannot replace Theo memory, durable
  child jobs, or schedules. Do not enable native tools that bypass the broker.
- Preserve one terminal outcome per attempt and distinct cancelled, interrupted,
  auth-wait, quota-wait, failed, and uncertain states. Cancellation revokes grants
  and stops owned process trees. Recovery validates process birth time as well as
  PID; never kill an unrelated process based on a stale PID alone.
- Native eligibility is bound to account, included model, runtime/configuration
  fingerprint, and fresh evidence. Codex obtains live subscription, catalogue,
  allowance and credit-state evidence before each turn and monitors it during
  inference; it does not require a daily manual attestation. Other adapters retain
  expiring operator evidence. Never turn native observations into claims about
  provider billing settings that were not observed. Keep hard stops and explicit operator retry
  after auth/quota recovery. Missing allowance data stays unknown, not zero.
  Do not weaken these gates to make a test or demo pass.

### Effects and delivery

- Put durable outbound effects through `Delivery` and its action/outbox ledger.
  Bind semantic identity to the exact content and destination. Keep approvals
  bound to the request hash, target, conversation, scope, and expiry.
- Tools declare an explicit `read`, `write`, or `outbound` effect policy.
  `write` calls reserve semantic receipts before execution; interrupted calls may
  remain uncertain. Outbound operations use the delivery ledger's own receipts.
- A possible remote acceptance without a receipt is **uncertain**. Do not resend,
  invent success, or clear uncertainty automatically. Reconciliation requires an
  actual receipt or confirmed no-effect for the exact ambiguous chunk. Use
  `NoEffect` only when the transport establishes that nothing happened.
- Successful chunks are never replayed. Keep cancellation, partial delivery,
  notification pauses, restored quarantine, and final conversation history
  consistent. Ephemeral typing/drafts are not durable final delivery evidence.
- Requested reminders remain deliverable during model/background pauses when the
  service/channel is available; delivery pauses and quarantine still apply.
  Preserve explicit timezone/DST semantics: skip nonexistent cron wall times and
  use the earlier instant once during a repeated wall time.

## Common change recipes

- **Add or change a model tool:** define its strict wire schema in
  `tools/schemas.py`, implement the owning `tools/handlers/` capability, bind a
  `ToolDefinition` with an explicit effect in `tools/registry.py`, and check
  group authorization. Regenerate `docs/tool-schemas.json` with
  `uv run --no-sync python scripts/export_tool_schemas.py`; update `docs/tools.md`.
  Test invalid input, stale/revoked grants, replay behavior, and the committed
  effect through the broker, not just by calling the handler directly.
- **Add a command or channel behavior:** put CLI syntax in `cli/parser.py`,
  operator execution in `cli/commands.py`, and conversation commands in
  `application/commands.py`. Keep Telegram normalization/rendering separate from
  admission/state and API sending. Exercise the corresponding terminal or
  Telegram path and update its guide.
- **Change native behavior or prompts:** start with `backends/base.py`, the
  specific adapter, and `memory/context.py`. Keep real protocol subprocess
  fixtures and terminal-outcome checks. Live stateful evaluation is a separate
  opt-in step; inspect actual tool results and receipts as well as answer quality.
- **Change defaults or configuration:** check both fresh and initialized roots.
  The persona in `storage.py` is seeded with `INSERT OR IGNORE`; editing its text
  does not update existing persona rows. `configure` replaces the saved settings,
  invalidates account attestations, and requires a daemon restart to take effect.
  Keep version changes consistent between `pyproject.toml` and `src/theo/__init__.py`;
  update `uv.lock` deliberately when package metadata or dependencies change.
- **Change observability:** edit instrumentation in `src/theo/observability/` and
  generate dashboards/rules with `uv run --no-sync python scripts/build_observability.py`.
  Commit the generated `observability/grafana/` changes together. Grafana UI edits
  are not the source of truth. Follow the [runbook](docs/observability.md) for
  correlation and dashboard-query validation when running the local stack.

## Testing strategy

Use the mapped test files for a focused iteration, then run the relevant broader
checks before handing off. The normal full verification commands match CI:

```sh
uv run --no-sync ruff check src tests scripts
uv run --no-sync ruff format --check src tests scripts
uv run --no-sync pyright
THEO_TEST_OFFLINE=1 HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 \
  uv run --no-sync pytest -q
uv build
```

For example, a broker change starts with
`uv run --no-sync pytest -q tests/test_tools_runtime.py tests/test_architecture.py`.

`tests/conftest.py` provides a temporary real SQLite `db`, synthetic `settings`, a
local `conversation`, and a controllable `clock`. Asyncio mode is automatic. An
autouse fixture sets `THEO_TEST_OFFLINE=1` and denies IPv4/IPv6 socket connects;
Unix sockets remain available for broker/protocol tests. Preserve these guards.
Test-owned subprocesses must retain the offline environment too.

Prefer observable invariants: query committed revisions, job generations, action
statuses, and delivery attempts. For durability changes, reopen the database or
exercise process death/recovery. For time behavior, advance the injected clock;
use events for concurrency ordering rather than arbitrary sleeps. Existing tests
use real subprocess protocol fixtures and Hypothesis where those expose boundary
failures. Add regressions for changed behavior, especially replay, cancellation,
scope, and partial failure; avoid tests that merely restate implementation details.

CI runs on standard `ubuntu-24.04` and `macos-14` runners and skips private
repositories. It installs browser/embedding Python extras, runs the checks above,
then installs the built wheel in a separate environment and runs
`scripts/check_installed_package.py` **outside the checkout**. That smoke check
imports production modules, verifies entry points, and applies bundled migrations;
running it in the editable `.venv` is invalid. Reproduce the exact install/export
steps in `.github/workflows/ci.yml` for packaging or module-layout changes. Keep
CI free of native accounts, Telegram credentials, model downloads, and live calls.

Live validation is documented in [docs/live-testing.md](docs/live-testing.md):

- `native_e2e.py` checks production adapters, canonical memory, and local receipts.
- `complex_e2e.py` checks stateful memory, reasoning, autonomy, scheduling, handoff,
  and voice. Run backend batches sequentially. Automated state checks and the
  separate transcript review/scorer are distinct evidence.
- `telegram_e2e.py` runs via `uv run --script` with its pinned user-client
  dependency; transport-only and model-backed modes prove different things.
- Native integration/behavior entry points require `--live` and reject
  `CI`, `GITHUB_ACTIONS`, or `THEO_TEST_OFFLINE`. Use a dedicated test bot/root for
  Telegram, with one poller per bot. Keep test-bot credentials out of Keychain,
  repository files, command arguments, logs, and reports; use the pairing helper's
  in-memory token or optional private token file.

Record the source commit/snapshot, backend/model, actual checks, and untested
scope. Preserve failed reports. A model saying it saved something, synthetic
protocol success, or Telegram transport acceptance alone does not prove the full
behavior. Never fabricate account attestations, receipts, human grades, or soak
time. Keep committed evidence synthetic and free of credentials/private content.

## Deployment, recovery, and observability

The deployment target is an awake Apple Silicon Mac with verified encrypted
storage and OS isolation. Linux offline CI does not qualify generated-code
execution. Runtime setup and operational commands live in
[docs/operations.md](docs/operations.md); qualification gates live in
`operations/qualification.py` and [docs/acceptance.md](docs/acceptance.md).

There are separate core, native runner home/workspaces, worker-shim environment,
and independent supervisor boundaries. Keep the readable, non-writable worker
environment outside the protected core data/release tree. Synchronize its
dependencies with the core lock when changing MCP or other runtime dependencies;
upgrading only the core leaves the shim on old code/dependencies.

The release path is operator driven; GitHub CI builds distributions and does not
deploy. `scripts/build_release.py` requires clean, committed source and produces a
locked relocatable installed environment, a hashed manifest, and an init/doctor
canary. `release-stage` verifies it. After pausing supervision and draining workers,
`upgrade --release ID` checks schema compatibility, takes a backup, and atomically
switches `releases/current`. Background work remains paused. `rollback` switches
compatible **application code**, never the database. `service install` generates a
launchd plist; loading it is a separate operator action. These commands and their
order are documented in the operations runbook, not an automatic development step.

Online backups include the exact reachable external blob set. Restore targets a
new root, quarantines outbound work, marks unconfirmed effects uncertain, and
invalidates accounts. Preserve explicit reconciliation and recovery release before
reactivation. Same-disk backups do not establish machine-loss recovery. Production
qualification requires actual native, isolation, behavior, capacity/restore, and
seven-day service evidence; passing CI or setting configuration flags cannot
substitute for it. Unattended self-patch deployment remains unqualified.

The optional local Grafana/Alloy/Prometheus/Loki/Tempo stack is separate from the
assistant runtime. Its **whole-host budget is 4,000,000,000 bytes**, including VM,
Docker, observer, and host helpers. The observer is read-only and must not create
or migrate a database. Keep telemetry queues/retention bounded and export failure
off the execution path. Exclude prompts, message text, tool arguments, credentials,
and arbitrary exception text from telemetry; retain safe correlation IDs. Preserve
unknown measurements and distinguish synthetic qualification traffic from live
activity. `observability/.env` and `.local/` are private, ignored local state.

## Finish a change

Review the diff, update affected docs/generated references, and report what changed,
which checks ran, and any concrete gaps. Keep historical evidence scoped to its
original run. Do not silently turn a source change into a daemon restart, live
message campaign, runtime qualification, or deployment. Commit only the intended
files; follow the user's requested branch, push, or PR workflow.
