# Telegram interface

Theo supports an owner private chat and explicitly configured group topics. Telegram commands, delivery, memory review and model tools use the same durable core as the terminal client.

## Set up a dedicated test bot

Create a bot with the verified [BotFather](https://t.me/BotFather), using `/newbot`. Run:

```sh
uv run --no-sync python scripts/telegram_setup.py --bot YOUR_TEST_BOT
```

Paste the token at the hidden prompt. The helper checks the bot identity and webhook state, prints a unique `/start pair-…` message, and waits for you to send it in the bot's private chat. That establishes the exact numeric owner and chat binding. It creates `~/Library/Application Support/Theo-Telegram-Test` by default and runs the normal daemon. Use `--data-root /absolute/test/root` to select a different test root; the helper's default is independent of the main CLI's platform-specific data root. The token remains in process memory; no token is saved in configuration, shell history or Keychain. Keep the terminal running; Ctrl-C stops the daemon. Restart by running the helper again and entering the token again. `THEO_TELEGRAM_TOKEN` is also supported for operator-managed environments.

Use `--token-browser` for a one-time loopback password form instead of the terminal prompt. The form checks its nonce, Host and Origin, disables caching/access logs, accepts one token and closes. `--presentation-check` sends a labelled synthetic typing/draft probe before starting the daemon, using the actual Telegram transport and delivery ledger. It does not call a model or qualify model execution. Stop any existing daemon for that test root first.

For repeatable local tests without Keychain, add `--save-token '/absolute/private/path/telegram-test.token'` during setup. After bot identity is verified, the helper creates the file with owner-only permissions and refuses to replace a different token. Later runs can use `--token-file` with that path. Token files must be regular files owned by the current user with no group or other access; symlinks are rejected. Keep the file outside the repository. `--pair-only` validates and saves the setup without starting a poller. Never run two pollers for the same bot.

Commands and requested reminders work without a model. Actual inference still needs normal native account and isolation setup from [operations.md](operations.md). This helper does not bypass those requirements or activate background autonomy.

## Groups and topics

Add the bot to a dedicated test group, enable topics if required, and add exact destinations to that root's configuration:

```json
"telegram_destinations": [
  {"chat_id": -1001234567890, "topic_id": 7},
  {"chat_id": -1001234567890, "topic_id": 8}
]
```

The IDs above are illustrative. Use the actual group and topic IDs. A `topic_id` of `0` means an unthreaded destination, not a wildcard. Restart after editing configuration. Preserve the configured owner/private-chat IDs. Only owner commands, owner mentions and owner replies to the bot invoke work in groups. Other members cannot invoke privileged controls. Telegram's own privacy and administrator settings determine which updates the bot can observe; individual and anonymous reaction updates are not universally available in every chat.

Each topic has its own conversation. Group retrieval excludes private memory, facts, personal instructions, pins, global commitments, and other conversations. Resource identifiers do not bypass this boundary. Private information can be shared from the private chat through an approved outbound action to a registered destination. Generated shell commands and global account/automation controls remain private-chat operations.

## Controls

- `/help`, `/status`, `/models`, `/backend [BACKEND MODEL]`, `/usage`.
- `/jobs [PAGE]`, `/cancel JOB_ID`, `/goals [PAGE]`.
- `/schedules [PAGE]`, `/remind ISO_TIME TEXT`, `/reschedule ID ISO_TIME`; natural-language requests also use the durable scheduler.
- `/memory [QUERY_OR_PAGE]` offers history, archive and restore buttons.
- `/review` presents action approvals and proposed memory/fact changes in the private chat.
- `/actions [PAGE]` exposes delivery details and uncertainty. Confirm no effect only after checking the destination yourself.
- `/delivered ACTION_ID CHUNK_ID [MESSAGE_IDS]` records an owner-verified receipt for an uncertain delivery; see the recovery instructions below.
- `/pause [background|models|notifications]` and `/resume [SCOPE]` retain the core's activation gates.

Pages are zero-indexed in commands and one-indexed in displayed labels. Button capabilities expire after an hour and bind to the actual review message, owner, operation and object state. Open a fresh view after expiry. Interrupted button operations are not blindly repeated; inspect the resulting object before retrying.

## Messages, media and delivery

Inbound updates are durably received before polling acknowledges them. Replies retain quoted context; edits retain immutable revisions. Editing queued work replaces its input. Editing work that already started fences that run and admits a correction; previous effects are preserved and cannot be automatically repeated by correction tools.

Albums wait for one second of inactivity, up to five seconds. Late members are separate follow-ups. Original media is retained; extraction state distinguishes ready, unsupported, oversized and failed. Speech uses local assets. Video uses at most eight timestamped samples and available audio transcription, with partial coverage disclosed.

Private chats show typing until visible answer text is available, then use ephemeral answer drafts and native Stop controls; groups use typing indicators. Typing refreshes at most every four seconds, and draft updates at most once a second. Drafts respect Telegram's UTF-16 size limit, including emoji. Only visible answer text enters previews. Work waiting for account verification or deliberately paused has no active generation to preview. Final messages and meaningful mutations retain outbox attempts and receipts. Long text prefers line or word boundaries while preserving every character. Rich rendering escapes model HTML and uses a limited formatting renderer. A definite formatting rejection permits a literal-text fallback; an ambiguous send remains uncertain. Successful chunks are never automatically replayed. Rich blocks beyond the renderer's supported subset remain literal text.

Replies preserve text, captions and structured rich content as bounded, untrusted context, alongside the referenced bot, chat, topic and message identity.

If an uncertain message did arrive, open `/actions` in your private chat and check its destination and content. Reply to the received message with `/delivered ACTION_ID CHUNK_ID`. Theo validates the referenced bot, chat and topic and records your confirmation without resending. For an album or another destination, append the checked message IDs, separated by commas: `/delivered ACTION_ID CHUNK_ID MESSAGE_IDS`. Every album member is required. Wrong destinations, existing unrelated message identities and reuse of another chunk's receipt are rejected; repeating the same confirmation is harmless. This is an explicit owner confirmation, not an independent Bot API lookup. The separate **Confirm no effect** control permits retry only after you have checked that nothing arrived.

## Diagnostics and evidence

```sh
uv run --no-sync theo --data-root '/absolute/test/root' telegram status
uv run --no-sync theo --data-root '/absolute/test/root' telegram doctor
uv run --no-sync theo --data-root '/absolute/test/root' telegram retry-event UPDATE_ID --bot-id BOT_ID
```

`status` reads local backlog and media health. `doctor` additionally checks the configured token, bot identity, webhook conflicts, membership and local media prerequisites; exception output omits token-bearing URLs. `retry-event` requeues a failed normalization event after its underlying issue has been addressed. Existing callback claims and message identities still prevent blind replay.

Offline contracts live in `tests/test_telegram_integration.py`. The [live test guide](live-testing.md) separates transport, model, media and client evidence. Implementation and offline passes do not establish live delivery, native model qualification or a seven-day production soak.
