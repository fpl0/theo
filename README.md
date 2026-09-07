# Theo

Theo is a new Python personal assistant built from ADR-0001 (7 September 2026). SQLite owns its memory and work history. Native subscription applications provide reasoning: Claude Code, Codex App Server, Cursor ACP and Grok ACP. The application contains no metered model API fallback.

**Status: implementation with deterministic integration tests; not production-qualified.** Native account canaries, target Mac isolation/service tests, local model assets, live behavioural grades and a genuine seven-day soak remain required. See the [acceptance report](docs/acceptance.md) and [requirement matrix](docs/requirements.md). Theo starts with no eligible accounts and background autonomy paused.

## Start locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
git clone https://github.com/fpl0/theo.git
cd theo
uv sync --frozen
uv run theo init
uv run theo doctor --json
uv run theo memory remember "I prefer concise, direct answers."
uv run theo memory search "concise"
uv run theo status
```

Python 3.14.6 is selected by `.python-version`. `uv.lock` pins the dependency graph. These commands do not call a model or send a Telegram message. macOS state defaults to `~/Library/Application Support/Theo`; Linux uses the XDG data directory. Put `--data-root /absolute/path` **before** the subcommand to select an isolated root.

For development:

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pyright
```

Tests use temporary databases, synthetic data and native-protocol subprocess fixtures. They do not log in, call paid services, contact model accounts or modify an existing assistant. Host-restricted OS tests report explicit skips.

## Connect a native runtime

1. Follow [the operator guide](docs/operations.md) to create a separate runner home, install a readable worker shim and verify the real OS boundary. A working directory alone is insufficient. Generated code remains unavailable without the qualified Mac sandbox.
2. Sign in using each vendor's supported **subscription** login. Disable extra usage, on-demand charging and top-ups in the account's native settings. Do not put API keys in Theo's environment.
3. Use `theo accounts verify BACKEND --evidence /path/to/evidence.json` to bind the account catalogue and billing evidence to the installed runtime fingerprint. The command reports the required fingerprint/configuration hash when evidence does not match. [Account evidence format](docs/operations.md#account-evidence).
4. Select a backend and an included model from that account's actual catalogue. For example, `theo chat --backend codex --model INCLUDED_MODEL "Hello Theo"`, then `theo serve` in the service session. `chat` durably queues input; `serve` processes it. No universal model name is hardcoded.

Telegram requires the exact numeric owner and chat IDs in configuration and the bot token in macOS Keychain service `theo.telegram` or `THEO_TELEGRAM_TOKEN`. Inbound updates are committed before the next polling acknowledgement. Local CLI conversations stay local even when Telegram is configured.

## What is implemented

- Revisioned SQLite memory, reviewed corrections, facts with validity intervals, FTS, optional local vectors, graph links, canonical checkpoints and inspectable context snapshots.
- Durable parent/child jobs, per-conversation serialization, lease generations, cancellation, quota/auth waiting states and truthful final-report obligations.
- All 33 baseline tool handlers plus scoped files, commands, artifacts, local voice creation, goals, fact proposals and reviewed skills. [Tool catalogue](docs/tools.md).
- Telegram text and rich media, input retention and local extraction, durable action approvals, chunk receipts and uncertain-send reconciliation.
- Timezone-aware schedules, DST rules, reminders independent of model capacity, eleven bounded autonomy loops and a separate critic for optional outreach.
- CLI administration, online backups, quarantined restores, immutable release builder/staging, compatible code rollback, an independent supervisor and optional narrow health alerts.
- Read-only Luke snapshot import, structured exports and a fixed 30-case behavioural evaluation pack.

Read [architecture and decisions](docs/architecture.md), [operations](docs/operations.md), [compatibility](docs/compatibility.json), [performance measurements](docs/capacity-results.json) and [remaining qualification work](docs/acceptance.md). Existing Luke source was inspected as reference evidence; Theo imports none of its modules and requires none of its files.
