# Theo

[![Quality checks](https://github.com/fpl0/theo/actions/workflows/ci.yml/badge.svg)](https://github.com/fpl0/theo/actions/workflows/ci.yml)

Theo is a personal assistant you run yourself.

It remembers what you tell it, and lets you correct it when it gets something wrong. It takes on work that outlives a single conversation, survives a restart in the middle of it, and tells you honestly whether it finished. It reaches you on Telegram or in your terminal, keeps track of what you asked for and when it is due, and asks before it acts in the world on your behalf.

Everything it knows lives in one SQLite database on your machine. Its thinking comes from native subscription apps you already pay for, so there is no metered model API and no per-token bill.

**Status: implementation with deterministic integration tests; not production-qualified.** Native account canaries, target Mac isolation/service tests, local model assets, live behavioural grades and a genuine seven-day soak remain required. See the [acceptance report](docs/acceptance.md) and [requirement matrix](docs/requirements.md). Theo starts with no eligible accounts and background autonomy paused.

## What Theo can do

**Remember you accurately.** Memory is revisioned rather than overwritten, so a correction is recorded as a correction and you can see what it used to think. Facts carry the period they were true for. Search combines full-text with optional local vectors, and every answer can show you the exact context it was built from.

**Finish what it starts.** Work is durable: a job survives a restart, a crash or a cancelled run, and picks up where it left off. Long tasks split into child jobs. When a run cannot finish because an account is out of quota or needs a login, Theo says that instead of inventing a result.

**Act on your behalf, with a check.** It reads and writes files in a scoped workspace, runs commands, keeps artifacts, creates voice notes and tracks goals. Actions that reach the outside world go through a durable approval step, and delivery is receipted, so a send whose outcome is uncertain gets reconciled rather than blindly retried. [Tool catalogue](docs/tools.md).

**Reach you where you are.** Telegram handles text, photos, documents and voice, and the terminal client gives you the same assistant locally. Reminders and schedules are timezone- and DST-aware and fire whether or not a model is available.

**Work on its own, within limits.** Bounded autonomy loops let it make progress between your messages, and a separate critic decides whether anything is worth interrupting you about.

**Stay recoverable.** Online backups, quarantined restores, staged immutable releases, code rollback and an independent supervisor, all driven from the CLI. Your data exports in structured form.

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
uv sync --locked --extra browser --extra embeddings
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pyright
```

Tests use temporary databases, synthetic data and native-protocol subprocess fixtures. They do not log in, call paid services, contact model accounts or modify an existing assistant. Host-restricted OS tests report explicit skips.

CI runs these checks plus distribution builds and an installed-package check on standard Linux and macOS GitHub runners. It uses no paid model services, persistent caches or artifact uploads, and skips if the repository becomes private. See [quality practices and review findings](docs/code-quality.md).

For real **Telegram → Theo → native model → Telegram** tests, use the [live test guide](docs/live-testing.md). It includes a runnable four-case suite, JSON/JUnit reports, and the results of an actual small local model experiment (1/4 initial checks passed).

## Interactive terminal

With Theo running, open a second terminal and run `uv run theo chat`. Paste or drag file paths, attach images with `/attach`, and read live Markdown/code output. Named sessions resume with `--session work`; Ctrl+C cancels a turn and `/quit` leaves the assistant running. See the [terminal guide](docs/terminal.md) for setup, attachments and commands.

## Connect a native runtime

1. Follow [the operator guide](docs/operations.md) to create a separate runner home, install a readable worker shim and verify the real OS boundary. A working directory alone is insufficient. Generated code remains unavailable without the qualified Mac sandbox.
2. Sign in using each vendor's supported **subscription** login. Disable extra usage, on-demand charging and top-ups in the account's native settings. Do not put API keys in Theo's environment.
3. Use `theo accounts verify BACKEND --evidence /path/to/evidence.json` to bind the account catalogue and billing evidence to the installed runtime fingerprint. The command reports the required fingerprint/configuration hash when evidence does not match. [Account evidence format](docs/operations.md#account-evidence).
4. Select a backend and an included model from that account's actual catalogue. For example, `theo chat --backend codex --model INCLUDED_MODEL "Hello Theo"`, then `theo serve` in the service session. `chat` durably queues input; `serve` processes it. No universal model name is hardcoded.

Telegram requires the exact numeric owner and chat IDs in configuration and the bot token in macOS Keychain service `theo.telegram` or `THEO_TELEGRAM_TOKEN`. Inbound updates are committed before the next polling acknowledgement. Local CLI conversations stay local even when Telegram is configured.

## Going deeper

Under the hood it is Python on SQLite, with reasoning supplied by Claude Code, Codex App Server, Cursor ACP or Grok ACP over their native protocols. Read [architecture and decisions](docs/architecture.md), [operations](docs/operations.md), [compatibility](docs/compatibility.json), [performance measurements](docs/capacity-results.json) and [remaining qualification work](docs/acceptance.md).

Claims here are meant to be checkable. [Captured evidence](docs/evidence/) holds the raw terminal recordings and JSON reports behind the testing statements above.

A read-only importer brings across a snapshot from an earlier assistant, Luke. That source was inspected as reference evidence only; Theo imports none of its modules and requires none of its files. A fixed 30-case behavioural evaluation pack measures answer quality.
