# Live testing Theo

The model-backed Telegram suite sends real Telegram messages through an already running Theo daemon and its normal native model adapter. It does not inject Bot API updates, replace the backend, modify SQLite, enroll accounts, or weaken isolation. The runner needs read access to that daemon's data directory, so run it on the same host under the operator account. Other sections below cover local native adapters, behavior, transport-only checks and the experimental local-model harness.

## Telegram → model → Telegram

Use a dedicated Theo test bot and data root. This suite creates four synthetic inputs, one persistent memory record and one small text document. It leaves the conversation and evidence intact for inspection. Keep other conversations and background work idle during the run.

1. Complete the [normal native setup](operations.md), including real OS isolation and verified subscription account evidence. Configure the exact allowed Telegram user/chat IDs and bot token. In a private bot chat, both allowed IDs are your user ID. Set `primary_backend` and `primary_model` in that root's `config.json`, or use `/backend BACKEND INCLUDED_MODEL` in the bot chat. Use your actual included model ID; the suite checks it exactly. Leave background autonomy paused, and models and notifications enabled.
2. Start the daemon in another terminal, using that root and its normal Telegram token configuration:

   ```sh
   uv run --no-sync theo --data-root /absolute/path/to/theo-test serve
   ```

3. Obtain a Telegram user-client application ID/hash from [Telegram's developer portal](https://my.telegram.org). Set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in your shell using your normal secret manager. These authenticate a Telegram **user**, not a model API. The suite never asks for a model API key or the bot token. Follow the [Telethon login flow](https://docs.telethon.dev/en/stable/basic/signing-in.html):

   ```sh
   uv run --script scripts/telegram_e2e.py --login-only
   ```

   Enter your phone number, Telegram login code and 2FA password when prompted. Use the account configured as Theo's owner. The session defaults to `~/.local/share/theo-e2e/user.session`; keep it private. `--session /absolute/path/user` selects another location and must be identical for login and test runs.

4. Run the suite, replacing the root, bot username and model:

   ```sh
   uv run --script scripts/telegram_e2e.py \
     --live \
     --data-root /absolute/path/to/theo-test \
     --bot @YOUR_THEO_TEST_BOT \
     --backend codex \
     --model YOUR_INCLUDED_MODEL \
     --timeout 180 \
     --output e2e-results
   ```

   The same suite supports `claude`, `cursor` and `grok` when configured through their normal adapters. It installs only its pinned Telethon test dependency; it does not modify Theo's dependency lock.

| Case | Evidence required |
| --- | --- |
| Model round trip | Fresh random marker returned by the selected native model, successful terminal event, final outbox receipt and matching message received by the Telegram user |
| Durable memory | Model invokes `remember`, successful receipt, and a memory revision from that run containing the synthetic fact |
| Memory retrieval | A subsequent model run invokes `recall`; its actual tool result and Telegram answer both contain the saved fact |
| Document input | Real Telegram file upload/download, registered artifact, extracted secret in canonical input, and correct model answer delivered back to Telegram |

Every case requires one native run, its canonical context ID, one successful terminal, one final action, and one successful attempt per delivery chunk. Auth/quota waits, missing tools, wrong routes, missing receipts and duplicates fail. Inputs have fresh per-run markers; old replies cannot satisfy the check. Telegram message IDs are account-specific, so the runner matches received text after its own input instead of comparing user-side IDs with bot-side receipt IDs.

Exit codes: **0** all four passed; **1** an executed case failed; **2** setup failed. `e2e-results/results.json` contains case results, job/run IDs and unrun cases; `junit.xml` supports test runners. Reports are checkpointed after every executed case. Setup failures print a diagnostic before any case report exists. The suite stops on the first failure to avoid accumulating model work on a blocked pipeline. Inspect that job and the daemon log, fix the cause, then rerun with fresh markers. It never retries an ambiguous send itself.

This covers the real text, memory and document path. It does **not** qualify seven-day operation, OS isolation, crash recovery, unauthorized-user rejection, voice transcription, image understanding or quota exhaustion. Those remain separate acceptance gates. A passing suite is not a production qualification override.

## Native MCP shim probe

The Telegram suite above needs a running daemon and a Telegram user session. `scripts/mcp_shim_probe.py` is a narrower check for the shim alone: it starts a throwaway broker on a real Unix socket, lets a real native app spawn `python -m theo.mcp_shim`, and passes only if a model-invoked `remember` reached SQLite. The grant is restricted to `remember`/`recall`, so the run cannot execute commands, send messages or touch an existing assistant. It does consume one included run on the account you select.

```sh
uv run --no-sync python scripts/mcp_shim_probe.py --live --backend claude --model YOUR_INCLUDED_MODEL \
  --output docs/evidence/mcp-shim-claude.json
uv run --no-sync python scripts/mcp_shim_probe.py --live --backend codex --model YOUR_INCLUDED_MODEL \
  --output docs/evidence/mcp-shim-codex.json
```

Both were executed on 7 September 2026 against `mcp` 2.1.1 while migrating the shim to the 2.x server API. Claude Code and Codex each discovered the tools, called them and persisted the run's marker; the captured reports are [mcp-shim-claude.json](evidence/mcp-shim-claude.json) and [mcp-shim-codex.json](evidence/mcp-shim-codex.json). Codex needs `--approve-for-me`, because `codex exec` otherwise refuses MCP tool calls under its default approval policy rather than asking. This probe covers the tool channel only. It is not Telegram evidence, not an isolation gate and not a production qualification.

## Local native adapter E2E

`scripts/native_e2e.py` exercises the production Codex app-server and Claude stream-JSON adapters through the real coordinator, a Unix-socket MCP broker, SQLite and a local delivery sink. Four cases verify a fresh response, a committed memory write, retrieval in a new conversation, and extracted document input. Each case also requires one terminal event and a final delivery receipt. A model merely saying it saved something cannot pass.

These tests consume included subscription usage and run **only when explicitly invoked locally with `--live`**. Native integration and behavior scripts refuse to run when `CI`, `GITHUB_ACTIONS` or `THEO_TEST_OFFLINE` is enabled. They are not collected by pytest or invoked by CI. CI only tests these refusal paths and synthetic protocol fixtures.

```sh
uv run --no-sync python scripts/native_e2e.py --live --backend codex --model YOUR_INCLUDED_MODEL \
  --output /tmp/theo-native-codex.json
uv run --no-sync python scripts/native_e2e.py --live --backend claude --model YOUR_INCLUDED_MODEL \
  --output /tmp/theo-native-claude.json
```

Sign in first using `codex login` or `claude auth login`. Remove any API/custom-route environment variables reported by the runner. It uses a throwaway data root and grants only `remember` and `recall`. The harness substitutes the operator's existing subscription authentication and native process launch for deployment attestation and OS-isolation checks; the actual adapter protocol, coordinator and tool/delivery code run unchanged. No production settings or account evidence are written. Passing is local integration evidence, not Telegram or production qualification.

On 8 September 2026, both local adapter suites passed **4/4** cases: [Codex evidence](evidence/native-e2e-codex.json), [Claude evidence](evidence/native-e2e-claude.json). The [review report](review-2026-09-08.md) records the bugs caught by these runs and the independent offline checks.

## Complex behavior and answer quality

The [9 September evaluation](complex-evaluation-2026-09-09.md) records **40 passing native turns and four host-state checks**, plus separate answer-quality reviews, for Codex `gpt-5.6-sol` and Claude `claude-opus-5`. Earlier Sonnet 4.5 quality failures are retained; its smaller smoke result does not qualify the complex suite.

`scripts/complex_e2e.py` runs at most **20 sequential native turns per batch**. Run the backends sequentially too. It creates real synthetic memories, correction proposals, goals, registered artifacts, child jobs, schedules, canonical checkpoints and local delivery receipts. The handoff cases switch the completed artifact conversation to the other native runtime and back after an owner fact correction. Each full batch therefore requires both subscription logins.

```sh
uv run --no-sync python scripts/complex_e2e.py --live --backend codex --model gpt-5.6-sol \
  --peer-model claude-opus-5 --output /tmp/theo-complex-codex.json
uv run --no-sync python scripts/complex_e2e.py --live --backend claude --model claude-opus-5 \
  --peer-model gpt-5.6-sol --output /tmp/theo-complex-claude.json
```

Use `--sections memory reasoning` to select an independent subset. Handoff requires `--sections autonomy handoff` so it uses an artifact actually produced by the primary model. A positive `--timeout` of at most 600 seconds bounds each turn; the default is 240. Remove any forbidden environment keys the script reports (for example `env -u NODE_OPTIONS …` on this development host). The script does not change native login or production configuration.

The suite covers memory correction/revision preservation, archive/restore, an optimal resource schedule with a known answer, conflicting and malicious source evidence, unknown facts and unperformed actions, autonomous invoice reconciliation, a durable delegated calculation, relative and recurring reminders, cancellation, model pause, empathetic responses, listening without advice, a clearer second explanation, and canonical continuity between models. Reminder checks advance the application clock; they are not a real-time soak or Telegram delivery test. Native capabilities that bypass Theo's broker are disabled in the Codex adapter, just as the Claude adapter disables built-in tools.

The raw report's `automated_pass` checks state and output oracles. It deliberately leaves `quality_review` unscored. A reviewer must inspect every native transcript against the fixed five-dimension rubric: correctness, evidence, completion, voice and judgment, each from 1 to 5. Acceptance requires every dimension to score at least 4, zero critical violations, and all automated checks to pass. These are subjective judgments about a small synthetic sample, not a general guarantee of model quality. Preserve failed reports and review notes when fixing a failure.

The offline scorer validates a separate review, binds it to the raw report's SHA-256, and rejects missing cases, low scores, critical violations or failed state checks:

```sh
uv run --no-sync python scripts/score_complex_report.py --report /tmp/theo-complex-codex.json \
  --review /path/to/transcript-review.json --output /tmp/theo-complex-codex-scored.json
```

The review JSON contains `reviewer`, `method`, and a `cases` list. Each case needs `name`, all five integer `scores`, concrete `notes`, and `critical_violations` (an empty list if none). The scorer never invokes a model. The older fixed thirty-case `evaluate_behaviour.py` also requires `--live` and rejects CI/offline execution; its scenario descriptions are a separate evaluation pack and are not counted as complex live passes.

## Small local model experiment

On 7 September 2026, actual CPU inference ran in the recorded test environment using [Qwen2.5-0.5B-Instruct Q4_K_M](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF), model revision `9217f5db79a29953eb74d5343926648285ec7e67`, with `llama-cpp-python==0.3.35`. No hosted model API was called.

The initial run passed **1/4** checks: Theo's identity. The other three generations copied an unrelated JSON context structure instead of producing the requested answer/tool object. The core recorded failed attempts with one terminal event and one local failure delivery each. This is evidence of a failed small-model/protocol experiment, not evidence that native Codex integration works. The initial raw generations are preserved in [local-model-initial-results.json](local-model-initial-results.json). The harness now reports invalid protocol objects explicitly instead of a generic `KeyError`; [local-model-results.json](local-model-results.json) records the rerun.

To reproduce with your own downloaded GGUF:

```sh
uv venv /tmp/theo-local-model --python 3.14
uv pip install --python /tmp/theo-local-model/bin/python \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu \
  'llama-cpp-python==0.3.35' -e .
/tmp/theo-local-model/bin/python scripts/test_local_model.py \
  --model /absolute/path/to/qwen2.5-0.5b-instruct-q4_k_m.gguf \
  --output e2e-results/local-model.json
```

Download the pinned file with `hf download Qwen/Qwen2.5-0.5B-Instruct-GGUF qwen2.5-0.5b-instruct-q4_k_m.gguf --revision 9217f5db79a29953eb74d5343926648285ec7e67 --local-dir /your/model/directory`. Set `HF_HUB_DISABLE_TELEMETRY=1`, `DO_NOT_TRACK=1` and `HF_HUB_DISABLE_IMPLICIT_TOKEN=1` for this public download. The recorded SHA-256 is in the result JSON.

This experimental adapter uses actual inference with a syntax-only JSON constraint, temporary synthetic SQLite state, the real coordinator and a broker restricted to `remember`/`recall`. Delivery uses an explicitly local in-process sink. It is not a new production backend and cannot execute commands or send Telegram messages. No native account or isolation setting is changed. The test returns nonzero on any failed check; a larger/different GGUF may behave differently.

## Recorded verification scope

The local inference experiment was executed. The Telegram suite's evidence verifier was tested offline against real Theo schema/delivery records and failure cases. The complete model-backed Telegram runner remains unrun. On 9 September, a dedicated bot was paired and the macOS Telegram interface was used for the partial client checks recorded below; these do not qualify the remaining native model or production boundaries.

## Expanded Telegram integration checks

The dedicated-bot helper in [telegram.md](telegram.md) pairs the private owner chat without saving the token by default; optional owner-only token-file storage is documented there. It starts the real daemon and leaves native account/isolation qualification unchanged.

The user-client runner now has a mode that needs no model configuration:

```sh
uv run --script scripts/telegram_e2e.py --live --transport-only \
  --data-root '/absolute/test/root' --bot @YOUR_TEST_BOT --output e2e-results/transport
```

It checks host controls, versioned edits, quoted replies, cancellation, and configured group/topic routing through actual Telegram messages. It pauses models during queued-input tests and restores them afterward. Use only a dedicated test bot/root. The same `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and authenticated owner session described above are required. Missing group bindings are explicitly reported as untested.

Model runs accept `--media-cases /absolute/fixtures/cases.json`. The manifest is a JSON list of objects with `name`, `path` (relative to the manifest), `kind`, `prompt`, and `expected`. Use synthetic fixtures only; the supplied files are uploaded to the dedicated bot. This exercises the actual upload, model run and receipted answer. Captionless Telegram types require separate client checks; do not count an unsupported client-library upload as a pass.

Example:

```json
[
  {"name":"photo","path":"red-square.png","kind":"photo","prompt":"What color is the square?","expected":"red"},
  {"name":"speech","path":"marker.ogg","kind":"voice","prompt":"Transcribe the spoken marker.","expected":"violet lantern"}
]
```

Before qualification, collect separate evidence for outgoing media and albums, approved/rejected/expired callbacks, poll answers/reactions, native draft rendering and Stop, notification pause, malformed-file handling, restart/ambiguous-send drills, and seven days of actual service observation. These remain explicit live gates; offline fixtures and the transport suite do not substitute for them.

## Actual client checks — 9 September 2026

[Recorded evidence](evidence/telegram-client-2026-09-09.json) covers the dedicated `@theo_fpl0_test_bot` in the macOS Telegram app. Owner pairing, commands, reminder delivery while models were paused, repeated cancellation taps, queued edits, document receipt, reply references and graceful restart persistence were observed through the client and checked against durable records. A labelled synthetic notification exercised the real typing/draft transport and final delivery ledger; no inference backend was substituted or called. Native Stop was not exercised before that probe finished.

The client exposed two presentation defects: empty private drafts gave no pre-answer indicator, and raw Rich HTML newlines collapsed. The fixes use typing until text is available and explicit line breaks consistent with [Telegram's Rich HTML format](https://core.telegram.org/bots/api#rich-html-style). The fixed indicator, growing draft and final paragraphs were then observed in Telegram. The final stream action has one delivery receipt.

The later [outbound, Stop and album checks](evidence/telegram-client-2026-09-09-outbound.json) used the actual client for a poll vote, rendered link, long reply, native Stop and an incoming photo/video album. All outgoing media cases received first-attempt delivery receipts. The Stop update cancelled the synthetic job, incremented its generation and cleared the preview record. The incoming album became one job with both originals and local audio transcription. Long reply chunks now preserve line/word boundaries, and draft limits account for emoji. Model inference, active model-process cancellation, remaining feedback/media checks, groups and the seven-day observation remain unqualified.

For reproducible outbound transport probes, keep the dedicated daemon running and queue one labelled case at a time:

```sh
.venv/bin/python scripts/telegram_outbound_probe.py --live \
  --data-root '/absolute/test/root' --chat-id YOUR_PAIRED_PRIVATE_CHAT_ID \
  --fixtures '/absolute/synthetic/fixtures' --tag YOUR_UNIQUE_TEST_TAG --case photo
```

Cases include `photo`, `document`, `voice`, `audio`, `video`, `animation`, `sticker`, `video_note`, `album`, `location`, `venue`, `contact`, `poll`, `links` and `long_text`. The script lists the expected synthetic filenames. Optional `--reply-to MESSAGE_ID` tests reply preservation. Reusing the same tag and case returns the existing action without resending it. This probe queues through the delivery ledger, never polls, and never calls a model. Inspect receipts and the real client separately; an accepted upload is not evidence of playback or semantic understanding.

### Lost-acknowledgement recovery

Stop the dedicated daemon and run `scripts/telegram_uncertainty_probe.py --live --data-root ROOT --token-file PRIVATE_TOKEN_FILE --bot TEST_BOT --chat-id PAIRED_PRIVATE_CHAT --tag UNIQUE_TAG`. The probe holds the daemon lock, refuses other queued deliveries, sends one labelled synthetic message through the real transport, and deliberately discards the successful result before the ledger records it. Two more dispatcher calls must leave it uncertain with one attempt. This is deliberate host fault injection after real remote acceptance, not a claim that Telegram suffered a network failure.

Restart the daemon, verify the received message in Telegram, and reply with the printed `/delivered ACTION_ID CHUNK_ID` command. Repeating the confirmation must preserve the single receipt and single send attempt. The probe refuses CI/offline execution, never polls or calls a model, and reusing its tag does not send again. The [9 September recovery evidence](evidence/telegram-client-2026-09-09-recovery.json) records the actual client reply, restart and repeated-confirmation checks.
