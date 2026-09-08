# Operating Theo

## Installation and isolation

Use the README's locked install first. The core works without eagerly loading browser or ML packages. Target deployment is an awake Apple Silicon Mac with owner-verified encrypted storage. `--encrypted-storage` is an operator attestation; it does not enable FileVault or encrypt SQLite itself.

Create a runner home outside the protected data root, with separate `tmp` and `workspaces` directories. Keep its native subscription logins there. Install a second locked Theo environment for the MCP shim in an OS-readable, non-writable runtime location outside the data root, such as an operator-managed `/opt/theo-worker`. Set `worker_python` to that environment's Python. The shim must be executable by the native worker even when the protected core/release directory is unreadable. Upgrade that environment from the same lock as the core whenever dependencies change: the shim runs there, not in the core environment, so a stale worker keeps an older `mcp` and fails at run start.

Export the existing JSON configuration, edit it locally, and apply it with:

```sh
uv run theo configure --file /absolute/path/config.json
uv run theo isolation verify
uv run theo doctor --json
```

`configure` validates fields and invalidates previous account attestations. Set `worker_home`, optionally `worker_python`, `primary_backend`, `primary_model`, exact Telegram IDs and `encrypted_storage_verified` only after checking them. Do not set production flags to bypass gates. The isolation command launches a real protected-read/write probe. Failure keeps execution disabled. The full target gate additionally requires credential, service-control, sibling-workspace, generated-code and process-cancellation checks; the simple probe alone does not establish those claims.

Generated commands require the Mac sandbox. They cannot change the protected database, supervisor, grants or native credential store. Browser work uses the broker's public-URL resolver, which denies non-global addresses, unsafe redirects, embedded credentials and unbounded responses. Generated commands do not receive general network permission.

## Account evidence

Log in through official native subscription workflows under the runner identity. Do not pass API keys or copy credential files into Theo's database. Inspect effective account billing controls, turn off extra usage/on-demand/top-ups, and establish that an included allowance ends at a hard stop. A display label or a balance alone does not prove eligibility.

The exact evidence fields are:

```json
{
  "account_ref": "owner-subscription",
  "label": "Owner native subscription",
  "pool_id": "actual-shared-allowance-pool",
  "models": ["ACTUAL_INCLUDED_MODEL_ID"],
  "runtime_version": "EXACT_CLI_VERSION",
  "fingerprint": "HASH_REPORTED_BY_VERIFY",
  "config_hash": "HASH_REPORTED_BY_VERIFY",
  "verification_method": "native_and_operator_attestation",
  "native_subscription_login": true,
  "extra_usage_disabled": true,
  "hard_stop_verified": true,
  "evidence": "Dated observations of native login, included catalogue and enforced billing controls; no secrets"
}
```

This is a format example, **not qualifying evidence**. `theo accounts verify claude --evidence /path/evidence.json` reports current version/fingerprints if they differ. Populate the evidence only from actual observations, then run verification again. Eligible evidence expires after 24 hours or any runtime/configuration change. Codex additionally checks App Server `account/read` for ChatGPT subscription authentication. Runtime upgrades must pass fresh contract canaries.

`theo accounts list`, `theo models list` and `/usage` distinguish unknown telemetry from exhausted allowance. After observing renewed included allowance, `theo accounts quota BACKEND --available` records that explicit operator confirmation across its shared pools. Reverify stale login/billing evidence, then inspect and resume with `theo jobs retry JOB_ID`. An uncertain effect must be reconciled first. There is no automatic paid or cross-provider fallback. Choose another verified route using `/backend BACKEND MODEL` or local `chat` flags.

## Channel and daily controls

Store the bot token in macOS Keychain using the Keychain application, service `theo.telegram`, or supply `THEO_TELEGRAM_TOKEN` to the core service. Tokens do not belong in repository files, configuration JSON, prompts or command-line arguments. Configure exact `telegram_owner_id` and `telegram_chat_id`. Start the service only after those bindings have been checked.

```sh
uv run theo serve
uv run theo status
uv run theo jobs list
uv run theo runs inspect RUN_ID
uv run theo actions inspect ACTION_ID
```

Telegram commands: `/status`, `/backend [BACKEND MODEL]`, `/models`, `/jobs`, `/cancel JOB_ID`, `/pause [background|models|notifications]`, `/resume [SCOPE]`, `/memory QUERY`, `/goals`, `/usage`, `/help`. Status and due reminders do not wait for model slots. Background resumption requires recorded production qualification; ordinary requested conversation work can run after account/isolation checks.

Inspect an action's target, request and hash before deciding:

```sh
uv run theo actions approve ACTION_ID --request-hash EXACT_REVIEWED_HASH
uv run theo actions reject ACTION_ID --request-hash EXACT_REVIEWED_HASH
uv run theo actions reconcile ACTION_ID --receipt /path/verified-receipt.json
uv run theo actions reconcile ACTION_ID --confirmed-no-effect
```

Only use `--confirmed-no-effect` after checking the remote destination. It authorizes retry of an effect known not to have occurred. Approval binds target, scope, request hash, chat and expiry. Never create receipts just to clear an uncertain queue. Telegram supports opaque approval callbacks, but the current operator approval UI is the CLI.

## Memory, facts and skills

```sh
uv run theo memory list
uv run theo memory show MEMORY_ID
uv run theo memory history MEMORY_ID
uv run theo memory edit MEMORY_ID --file /path/reviewed.txt --expected-revision 2
uv run theo memory review CORRECTION_ID --accept
uv run theo memory archive MEMORY_ID
uv run theo memory restore MEMORY_ID --revision 1
uv run theo facts set owner residence Dublin --expected-revision 0
uv run theo skills list
uv run theo skills evaluate SKILL_ID --cases /path/cases.json
uv run theo skills activate SKILL_ID
uv run theo skills rollback SKILL_ID
```

Skill cases are at least three objects with `input`, `expected`, `observed` and boolean `passed`. They are operator-recorded observable results; the system does not fabricate a score. Only active skills with matching narrow triggers enter context. `fact_propose` preserves source evidence for review; authoritative fact changes use `facts set` with the current revision. CLI `memory erase` removes active records and invalidates materialized contexts. Separately retained messages, provider transcripts and old backups need their own retention/erasure decisions.

## Local assets and media

```sh
uv sync --frozen --extra embeddings --extra browser --extra speech
uv run --extra embeddings theo assets install-embeddings
uv run --extra browser theo assets install-browser
uv run --extra embeddings theo assets repair-embeddings
```

The embedding installer downloads BGE-base-en-v1.5 and records file hashes. Hugging Face telemetry is disabled before loading the downloader. Runtime inference loads local files only. No model-download verification succeeded in the build environment; see the acceptance report. FTS remains available without assets. Re-run the locked `uv sync` with required extras when operating a provisioned runtime.

For speech, provision a compatible local MLX Whisper model directory at `DATA_ROOT/models/speech`; this release does not silently download a speech model. Install FFmpeg through the operator's existing package manager. macOS `say` plus FFmpeg creates voice responses locally. Original audio remains available when transcription is unavailable. PDF, text, Office ZIP, photo, location and video descriptors are retained; image inputs are bounded/normalized before native transport. Video understanding is limited to a first-frame preview in this release.

## Backup, export, import and restore

```sh
uv run theo backup create
uv run theo backup verify /path/snapshot
uv run theo memory export --format jsonl --output /path/theo-export.jsonl
uv run theo memory export --format markdown --output /path/theo-memory.md
uv run theo import luke --source /path/read-only-snapshot --dry-run
uv run theo import luke --source /path/read-only-snapshot --apply
uv run theo restore --source /path/snapshot --target /path/new-quarantined-root
```

Online SQLite backups include integrity checks, a database hash and the exact reachable external blob manifest. Default retention is 24 recent snapshots plus one per day for 14 days. Successful hourly backups target an RPO of one hour. A separate owner-provided disk/destination is necessary for machine loss; same-disk snapshots only protect against logical mistakes.

Restore requires a new target directory and starts with all outbound work quarantined, notifications/background paused, pending work uncertain and accounts requiring re-verification. Reconcile effects since the snapshot before deliberate reactivation. Automatic database rollback is prohibited. JSONL exports contain complete structured rows and inline binary bodies; use the full backup for portable external media.

Use `theo --data-root RESTORED_ROOT recovery inspect` to review the snapshot time, uncertain jobs and actions. Reconcile remote effects and cancel unresolved restored jobs. `recovery release --snapshot-time EXACT_REVIEWED_TIMESTAMP` then releases outbound quarantine; it refuses while uncertainty remains and keeps background work paused.

The Luke importer reads a disconnected snapshot only, hashes sources, preserves accepted body history/tombstones, quarantines ambiguous mismatches, maps imported IDs and creates paused schedules. It does not execute imported scripts. Dry-run unsupported types must be reviewed. It cannot invent unavailable private persona/plan/session data or guarantee parity with uninspected Luke schema variants.

## Supervisor, releases and rollback

```sh
uv run theo service install --output /path/local.theo.supervisor.plist
uv run theo service pause
uv run theo service resume
```

`service install` generates the launchd definition; it does not load it or cut over another assistant. Use `launchctl bootstrap` in the intended user session after target checks. `maintenance.pause` is independent of the core and suppresses restarts. The supervisor writes its own heartbeat, restarts with capped backoff and opens a circuit after five failures in an hour. It terminates owned descendant processes before recovery. A separate optional health bot credential uses Keychain service `theo.health` or `THEO_HEALTH_TOKEN`; it can only send minimal core-health alerts to the configured owner chat. A whole-machine/network outage cannot be reported by this same-machine process.

Build from committed, clean source:

```sh
uv run python scripts/build_release.py --destination /absolute/path/theo-release --id VERSION-COMMIT
uv run theo release-stage /absolute/path/theo-release
uv run theo service pause
uv run theo jobs list
uv run theo upgrade --release VERSION-COMMIT
uv run theo service resume
```

The builder creates a locked, installed, relocatable Python environment and runs an isolated init/doctor canary. Stage verifies file hashes. Switching requires drained workers, compatible schema and a pre-switch backup. The `current` pointer is atomic and background work stays paused. `rollback --release PREVIOUS_ID` switches compatible application code; it does not restore a database. Keep the independent supervisor environment and worker shim outside the protected release directory. Native canaries and automatic self-patch regression recovery remain separate target gates.

## Qualification and troubleshooting

`theo qualification status` explains each production gate. `qualification record --file REPORT.json` accepts operator-only, source-attributed evidence with a `kind`, optional `backend`, and `evidence` object. Recognized kinds are `native_canary`, `mac_deployment`, `behaviour`, `seven_day_soak`, `capacity_restore`, `deterministic`. The validator in `qualification.py` specifies required observable fields. Record actual results; do not pre-fill passing reports.

Run the fixed behavioural pack in a dedicated `--owner evaluation` root with verified accounts and isolation:

```sh
uv run python scripts/evaluate_behaviour.py --data-root /path/evaluation-root --backend claude --model INCLUDED_MODEL --limit 20 --output /path/claude-evaluation.json
```

The runner is sequential, preserves exact context snapshots and outcomes, and resumes unfinished cases in later batches. It never dispatches evaluation messages. Grades remain null until human review. Preserve failed attempts. Require all 30 cases, at least 90% acceptable outcomes and zero critical violations. Behaviour prompts use synthetic scenario descriptions; deterministic tests establish actual host-state invariants separately. Native handoff, cancellation and rich-media canaries must additionally exercise real provider behaviour.

Run `scripts/benchmark.py --output /path/capacity.json` for the reproducible lexical reference fixture. Repeat on the target with warm local vectors for the full service objective. A real seven-day soak must measure awake/service-enabled time, exclusions, failures, queue delays, backups and recovery; elapsed wall time cannot be substituted with a fake clock.

Useful diagnostics: `doctor --json`, `status`, `jobs/runs/actions inspect`, `heartbeat.json`, `supervisor-heartbeat.json`, `supervisor-alert.json`, structured `health_events`, and embedding job lag. Auth/config drift parks work. Missing assets preserve lexical context/input. Socket or UID-transition denial means the host cannot qualify that boundary; it is not an invitation to disable isolation. On a failed migration, preserve the database and inspect checksum/schema state before recovery.
