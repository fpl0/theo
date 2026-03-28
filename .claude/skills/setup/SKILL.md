---
name: setup
description: First-time Theo setup and onboarding. Use when the user wants to install dependencies, configure infrastructure, set up Telegram, or get Theo running for the first time. Triggers on "setup", "install", "onboard", "getting started", or first-time setup requests.
user-invocable: true
---

# Theo Setup

You are the setup wizard for Theo — a personal AI agent built for decades of continuous use.

Your job is to get the user from zero to a fully running Theo instance with observability, database, and a Telegram interface — all verified and working.

## Principles

- **Run commands yourself.** Don't tell the user to run things — do it for them.
- **Fix problems yourself.** If something fails, diagnose and fix it. Only escalate to the user when it genuinely requires their action (credentials, external accounts).
- **Use `AskUserQuestion` for all user-facing questions.** Never use plain text for questions that need answers.
- **Detect existing state.** Before each step, check if it's already done. Skip silently if so. This makes the skill safe to re-run on partial setups.
- **Show progress.** After completing each phase, print a brief status line so the user knows where they are.

## Phase 0 — Welcome

Print this banner:

```
Theo Setup
----------
I'll get everything running for you. This takes about 5 minutes.

Phases:
  1. Prerequisites     — Python, uv, Docker, just
  2. Dependencies      — install packages
  3. Infrastructure    — PostgreSQL + OpenObserve
  4. Configuration     — API keys + environment
  5. Telegram          — bot setup (optional)
  6. Verification      — run checks, test connectivity
  7. IDE               — editor extensions (optional)

Let's go.
```

## Phase 1 — Prerequisites

Check each tool. Install what's missing. Fail only if something can't be auto-installed.

```bash
python3 --version   # Need 3.14+
uv --version        # Need uv
docker --version    # Need Docker
just --version      # Need just
```

**Resolution table:**

| Tool | If missing |
|------|-----------|
| Python < 3.14 | `brew install python@3.14` (macOS). If not on macOS, tell user what to install. |
| uv | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Docker | AskUserQuestion: "Docker is required for PostgreSQL and OpenObserve. Please install Docker Desktop from https://www.docker.com/products/docker-desktop/ and start it, then tell me when you're ready." |
| just | `brew install just` (macOS) or `cargo install just` |

After all pass, print:

```
[1/7] Prerequisites    OK
```

## Phase 2 — Dependencies

```bash
cd /Users/fpl0/Code/theo && uv sync
```

If it fails: delete `.venv` and retry once. If it still fails, show the error.

After success:

```
[2/7] Dependencies     OK
```

## Phase 3 — Infrastructure

Check if containers are already running:

```bash
cd /Users/fpl0/Code/theo && docker compose ps --format json
```

If both `postgres` and `openobserve` are healthy, skip. Otherwise:

```bash
cd /Users/fpl0/Code/theo && docker compose up -d
```

Wait for health checks (poll `docker compose ps` every 2 seconds, up to 30 seconds):

```bash
# Poll until postgres is healthy
docker compose ps --format json | python3 -c "import sys,json; data=json.loads(sys.stdin.read()); print('healthy' if all(s.get('Health','')=='healthy' or s.get('State','')=='running' for s in (data if isinstance(data,list) else [data])) else 'waiting')"
```

After both are healthy, provision dashboards:

```bash
cd /Users/fpl0/Code/theo && just dashboards
```

This provisions dashboards and alerts in OpenObserve. Alerts may be skipped if no data streams exist yet (they'll be created on the next run).

```
[3/7] Infrastructure   OK  (PostgreSQL :5432, OpenObserve :5080, dashboards provisioned)
```

## Phase 4 — Configuration

Check if `.env.local` exists and has the required vars. Build it incrementally — don't overwrite existing values.

### 4a. Database URL

Check if `THEO_DATABASE_URL` is set in `.env.local`. If not, add the default:

```
THEO_DATABASE_URL=postgresql://theo:theo@localhost:5432/theo
```

### 4b. Observability defaults

Ensure these are in `.env.local` (add any that are missing):

```
THEO_LOG_LEVEL=DEBUG
THEO_OTEL_ENABLED=true
THEO_OTEL_EXPORTER=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:5080/api/default
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic dGhlb0B0aGVvLmRldjp0aGVv
```

### 4c. Anthropic API key

Check if `THEO_ANTHROPIC_API_KEY` is in `.env.local`. If not:

AskUserQuestion: "I need your Anthropic API key to power Theo's LLM. You can get one at https://console.anthropic.com/settings/keys\n\nPaste your API key (it starts with `sk-ant-`):"

Write it to `.env.local`:

```
THEO_ANTHROPIC_API_KEY=<key>
```

After all config is set:

```
[4/7] Configuration    OK
```

## Phase 5 — Telegram (optional)

AskUserQuestion: "Do you want to set up Telegram as Theo's interface?\n\nThis lets you chat with Theo from your phone. You'll need a Telegram account.\n\n- **yes** — I'll walk you through it (~2 minutes)\n- **skip** — you can set this up later with `/setup`"

If skip: print `[5/7] Telegram          SKIPPED` and move on.

If yes:

### 5a. Bot token

AskUserQuestion: "Do you already have a Telegram bot token, or should I walk you through creating one?\n\n- **I have a token** — paste it below\n- **Create new** — I'll guide you through BotFather"

**If creating new**, tell the user:

```
Here's how to create your bot:

1. Open Telegram and search for @BotFather
2. Send /newbot
3. Name it whatever you like (e.g., "Theo")
4. Choose a username (must end in "bot", e.g., "theo_personal_bot")
5. BotFather will give you a token — paste it here
```

Then AskUserQuestion: "Paste the bot token from BotFather:"

**Verify the token works:**

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getMe" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Bot verified: @{d[\"result\"][\"username\"]}') if d.get('ok') else print(f'ERROR: {d}')"
```

If invalid, tell the user and ask again (once). If still invalid, save what they gave and note the issue.

Write to `.env.local`:

```
THEO_TELEGRAM_BOT_TOKEN=<token>
```

### 5b. Owner chat ID

AskUserQuestion: "Now I need your Telegram chat ID so Theo only responds to you.\n\nSend **any message** to your new bot in Telegram, then tell me when you've done it."

Poll for the message:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates?timeout=30" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('result'):
    msg = data['result'][-1].get('message', {})
    chat_id = msg.get('chat', {}).get('id')
    name = msg.get('from', {}).get('first_name', 'Unknown')
    print(f'FOUND chat_id={chat_id} name={name}')
else:
    print('NO_MESSAGES')
"
```

If no messages after polling, ask the user to try again (once).

Write to `.env.local`:

```
THEO_TELEGRAM_OWNER_CHAT_ID=<chat_id>
```

Clear the update queue so Theo doesn't replay the setup message:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates?offset=-1" > /dev/null
```

```
[5/7] Telegram          OK  (bot: @<username>, owner: <name>)
```

## Phase 6 — Verification

Run a comprehensive check to confirm everything works.

### 6a. Database connectivity

```bash
cd /Users/fpl0/Code/theo && uv run python -c "
import asyncio, asyncpg
async def check():
    conn = await asyncpg.connect('postgresql://theo:theo@localhost:5432/theo')
    version = await conn.fetchval('SELECT version()')
    extensions = await conn.fetch(\"SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_stat_statements')\")
    print(f'PostgreSQL: {version.split(chr(44))[0]}')
    for ext in extensions:
        print(f'Extension: {ext[\"extname\"]}')
    await conn.close()
asyncio.run(check())
"
```

### 6b. OpenObserve reachable

```bash
curl -s -o /dev/null -w "%{http_code}" -u "theo@theo.dev:theo" http://localhost:5080/api/default/organizations
```

Should return 200.

### 6c. Quality gate

```bash
cd /Users/fpl0/Code/theo && just check
```

If any check fails, diagnose and fix it (formatting issues can be fixed with `just fmt`, others need investigation).

### 6d. Quick smoke test

Start Theo briefly to verify it boots and connects:

```bash
cd /Users/fpl0/Code/theo && timeout 10 uv run theo 2>&1 || true
```

Look for "startup complete" or "running" in the output. If it exits with config errors, the relevant env var is missing — go back and fix it.

```
[6/7] Verification     OK
```

## Phase 7 — IDE Setup (optional)

Check if VS Code is available:

```bash
code --version 2>/dev/null
```

If available:

AskUserQuestion: "Want me to install recommended VS Code extensions for Theo?\n\nThis includes:\n- **astral-sh.ruff** — Python linter/formatter\n- **astral-sh.ty** — type checker\n- **ms-python.python** — Python language support\n\n- **yes** / **skip**"

If yes:

```bash
code --install-extension astral-sh.ruff
code --install-extension astral-sh.ty
code --install-extension ms-python.python
```

```
[7/7] IDE              OK
```

If VS Code not available or skipped:

```
[7/7] IDE              SKIPPED
```

## Finish

Print the final summary:

```
Setup complete!
===============

Theo is ready. Here's what you need to know:

  Start everything:     just dev
  Stop containers:      just down
  Run quality checks:   just check
  View telemetry:       http://localhost:5080  (theo@theo.dev / theo)
  Database shell:       just psql

Your config is in .env.local (gitignored, safe for secrets).

To start chatting, run `just dev` and send a message to your bot on Telegram.
```

## Troubleshooting

If anything fails during setup, diagnose and attempt to fix it yourself. Common issues:

| Problem | Fix |
|---------|-----|
| Docker not running | Ask user to start Docker Desktop |
| Port 5432 in use | `lsof -i :5432` to find the process, ask user before killing |
| Port 5080 in use | `lsof -i :5080` to find the process, ask user before killing |
| `uv sync` fails | Delete `.venv`, retry. Check Python version. |
| Bot token invalid | Verify with Telegram API, ask for re-entry |
| Database connection refused | Check `docker compose ps`, restart if unhealthy |
| `just check` lint failures | Run `just fmt` to auto-fix, then re-check |
