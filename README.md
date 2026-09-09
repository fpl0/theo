# Theo

[![Quality checks](https://github.com/fpl0/theo/actions/workflows/ci.yml/badge.svg)](https://github.com/fpl0/theo/actions/workflows/ci.yml)

Theo is a personal assistant you run yourself, with persistent memory, durable work and conversations in Telegram or your terminal.

It remembers what you tell it, records corrections without losing history, and keeps track of jobs, goals and reminders beyond a single conversation. You can inspect what it used, what it changed and whether an action actually completed.

Theo keeps canonical memory and work state in SQLite on your machine, with large media stored as content-addressed files. Reasoning comes from Claude Code, Codex App Server, Cursor ACP or Grok ACP through their native subscription runtimes. Theo requires verified included usage and has no metered model API fallback. Selected context and attachments are sent to the chosen provider; local storage does not mean local-only inference.

**Status — 9 September 2026:** implemented, with offline integration coverage, successful local Codex/Claude evaluations, real Telegram client checks and a locally tested observability stack. **Production qualification remains incomplete.** A fresh installation has no verified accounts and starts with background autonomy paused. See [current acceptance status](docs/acceptance.md) for the evidence and remaining gates.

## What Theo can do

- **Remember with history.** Revisioned memories, reviewed corrections, time-bounded facts, archive/restore and lexical search with optional local embeddings. Each model run records its selected context and sources.
- **Keep work durable.** Jobs, child jobs, goals and checkpoints survive process restarts. Cancellation stops owned work; authentication, quota and uncertain effects remain visible for recovery instead of being reported as success.
- **Use Telegram day to day.** Owner private chat and configured groups/topics; replies, edits, albums, typing and private answer drafts; rich messages, media, in-chat approvals, memory review, job controls and delivery reconciliation. Group context and tools enforce private-memory boundaries. [Telegram guide](docs/telegram.md).
- **Chat locally.** An interactive terminal with Markdown, code highlighting, attachments, live drafts, named sessions and cancellation. Terminal conversations stay separate from Telegram and deliver locally. [Terminal guide](docs/terminal.md).
- **Schedule reliably.** Requested reminders and recurring schedules use the owner's timezone and explicit daylight-saving rules. Due reminders can deliver while models or background autonomy are paused, provided the service and channel are available.
- **Work through controlled tools.** Public-web reads, scoped files and commands, artifacts, local voice creation, goals and delegation. Cross-destination sends, forwards and deletions require approval. Outbound actions retain receipts; an ambiguous send requires reconciliation before retry. [All 51 tools](docs/tools.md).
- **Operate and investigate.** Online backups, quarantined restores, structured export, staged releases, code rollback and a separate supervisor. Optional Grafana dashboards connect logs, traces, metrics and test-bot alerts. [Operations](docs/operations.md) · [Observability](docs/observability.md).

Background autonomy, reflection and reviewed skills are implemented, with activation gated by production evidence. Generated commands require verified macOS isolation. Automatic provider failover, unlimited semantic history compaction and unattended self-patch deployment remain incomplete.

## Start locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
git clone https://github.com/fpl0/theo.git
cd theo
uv sync --locked
uv run --no-sync theo init --timezone Europe/Dublin
uv run --no-sync theo doctor --json
uv run --no-sync theo memory remember "I prefer concise, direct answers."
uv run --no-sync theo memory search "concise"
uv run --no-sync theo status
```

Choose your IANA timezone at initialization. Python 3.14 is selected by `.python-version`; `uv.lock` pins dependencies. These commands initialize and inspect local state without calling a model or sending messages. Missing account/isolation/qualification checks in `doctor` are expected on a fresh root.

On macOS, state defaults to `~/Library/Application Support/Theo`; on Linux, `$XDG_DATA_HOME/theo` or `~/.local/share/theo`. Put `--data-root /absolute/path` **before** the subcommand to use another root, and use that same root for the daemon and client. Apple Silicon macOS is the deployment target; Linux supports core development and offline tests but is not the qualified generated-code runtime.

### Enable model conversations

1. Follow [installation and isolation](docs/operations.md#installation-and-isolation) to configure a separate native runner home and a readable worker environment for the MCP shim. Run `theo isolation verify` against that setup.
2. Sign in through the selected vendor's subscription login under the runner identity. Verify included models and spending controls, with extra usage, on-demand charging and top-ups disabled.
3. Record genuine [account evidence](docs/operations.md#account-evidence) using `theo accounts verify BACKEND --evidence /path/evidence.json`. Evidence must match the installed runtime and effective configuration; it expires after 24 hours or drift.
4. Start the daemon and open the client in separate terminals:

   ```sh
   # Terminal 1
   uv run --no-sync theo serve

   # Terminal 2 — replace YOUR_INCLUDED_MODEL with a verified catalogue entry
   uv run --no-sync theo chat --backend codex --model YOUR_INCLUDED_MODEL
   ```

Use `--session work` to resume a named interactive conversation. Passing text, as in `theo chat "Hello Theo"`, queues a message and returns JSON; the running daemon processes it. The default route comes from `primary_backend` and `primary_model` in configuration. Interactive route choices apply to that conversation.

### Connect Telegram

Create a dedicated bot with [BotFather](https://t.me/BotFather), then pair its private chat:

```sh
uv run --no-sync python scripts/telegram_setup.py --bot YOUR_TEST_BOT
```

The helper prompts for the token without echoing it, verifies bot identity, and asks you to send a unique pairing message. It uses a separate `Theo-Telegram-Test` data root and starts the normal daemon. By default, the token stays in process memory. Optional private token files support repeat runs without Keychain; see [setup and controls](docs/telegram.md). Keep one poller per bot, and configure native accounts/isolation for this separate root before expecting model replies. Host commands and requested reminders work without inference.

### Optional local assets

The base install supports lexical memory search and local document extraction. Install the extras you need:

```sh
uv sync --locked --extra embeddings --extra browser --extra speech
uv run --no-sync theo assets install-embeddings
uv run --no-sync theo assets install-browser
```

The asset commands download embedding weights and a browser respectively. Speech transcription requires Apple Silicon, a separately provisioned local MLX Whisper model and FFmpeg; voice creation uses macOS `say` and FFmpeg. Telegram video extraction uses at most eight timestamped samples and available audio transcription, with partial coverage disclosed. See [assets and media](docs/operations.md#local-assets-and-media).

## What has been verified

These are scoped, dated results; they do not qualify every current feature or deployment.

| Area | Recorded evidence |
| --- | --- |
| Native adapters | Codex and Claude each passed four local response/memory/document cases through the production adapters, coordinator, MCP broker and local delivery. [8 September review](docs/review-2026-09-08.md). |
| Complex behavior | Codex gpt-5.6-sol and Claude Opus 5 passed 40 native turns, four host-state checks and separate transcript review on a frozen source snapshot. Telegram edits were excluded. [9 September evaluation](docs/complex-evaluation-2026-09-09.md). |
| Telegram | Real client checks cover controls, reminders, edits, media intake/delivery, approvals, memory review, synthetic drafts/Stop and lost-acknowledgement recovery. Model-backed Telegram, live groups/topics and remaining media/feedback checks are still pending. [Implementation status](docs/telegram-implementation.md). |
| Observability | A ten-minute local load test with 8,680 synthetic operations peaked at about 1.82 GB for the whole stack, within its 2 GB budget. [Runbook and evidence](docs/observability.md). |

The [acceptance report](docs/acceptance.md) tracks current offline verification, native spending-control and deployment requirements, asset coverage, and the outstanding seven-day service soak. The [evidence index](docs/evidence/README.md) links raw reports, including failed and partial runs.

## Development and documentation

```sh
uv sync --locked --extra browser --extra embeddings
THEO_TEST_OFFLINE=1 HF_HUB_OFFLINE=1 uv run --no-sync pytest -q
uv run --no-sync ruff check src tests scripts
uv run --no-sync ruff format --check src tests scripts
uv run --no-sync pyright
uv build
```

Tests use temporary state, synthetic data and native-protocol subprocess fixtures. Live native entry points require `--live` and reject CI/offline environments. CI runs lint, formatting, types, tests, distribution builds and an installed-package check on standard Linux/macOS runners; it skips private repositories and uses no model accounts, persistent caches or artifact uploads. [Quality practices](docs/code-quality.md) · [Live test guide](docs/live-testing.md).

Start with the [documentation index](docs/README.md) for user guides, architecture, the tool catalogue and qualification records. The [source organization guide](docs/architecture.md#source-organization) maps the responsibility-based packages and contribution rules. Theo is an independent implementation; its read-only Luke importer accepts disconnected snapshots and does not depend on Luke at runtime.
