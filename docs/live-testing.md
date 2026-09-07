# Live testing Theo

The live suite sends real Telegram messages through an already running Theo daemon and its normal native model adapter. It does not inject Bot API updates, replace the backend, modify SQLite, enroll accounts, or weaken isolation. The runner needs read access to that daemon's data directory, so run it on the same host under the operator account.

## Telegram → model → Telegram

Use a dedicated Theo test bot and data root. This suite creates four synthetic inputs, one persistent memory record and one small text document. It leaves the conversation and evidence intact for inspection. Keep other conversations and background work idle during the run.

1. Complete the [normal native setup](operations.md), including real OS isolation and verified subscription account evidence. Configure the exact allowed Telegram user/chat IDs and bot token. In a private bot chat, both allowed IDs are your user ID. Set `primary_backend` and `primary_model` in that root's `config.json`, or use `/backend BACKEND INCLUDED_MODEL` in the bot chat. Use your actual included model ID; the suite checks it exactly. Leave background autonomy paused, and models and notifications enabled.
2. Start the daemon in another terminal, using that root and its normal Telegram token configuration:

   ```sh
   uv run theo --data-root /absolute/path/to/theo-test serve
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

## Small local model experiment

On 7 September 2026, actual CPU inference ran here using [Qwen2.5-0.5B-Instruct Q4_K_M](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF), model revision `9217f5db79a29953eb74d5343926648285ec7e67`, with `llama-cpp-python==0.3.35`. No hosted model API was called.

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

## What was verified here

The local inference experiment was executed. The Telegram suite's evidence verifier was tested offline against real Theo schema/delivery records and failure cases. **The live Telegram suite has not been run here.** The hosted environment cannot exercise Theo's required Unix socket/OS boundary, and no dedicated Telegram user session was configured. Run the command above on the deployment host for actual native end-to-end evidence.
