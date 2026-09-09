# Telegram implementation status — 9 September 2026

The integration is implemented, with partial live validation through the dedicated test bot and macOS Telegram client. Full model-backed, group/topic and production qualification remain incomplete. This dated report preserves the client campaign results; see [acceptance status](acceptance.md) for the current checkout verification.

Implemented and covered by offline contracts:

- Migration 003, destination/message identity, historical binding backfill, durable receipt and bounded normalization retries.
- Versioned edits, stale-worker fencing, prior-effect preservation, reply evidence, album collection and topic routing.
- Private-chat review cards, message-bound callback capabilities, cancellation, memory history/archive/restore, fact/correction review, reminders and rescheduling.
- Private streaming drafts, native Stop handling, group typing, escaped rich output and conservative delivery fallback.
- Common incoming/outgoing media, file-reference cache, bounded timestamped video samples, retained invalid originals and extraction states.
- Observed reaction/poll feedback and conversation attribution.
- Group memory/resource isolation in context assembly and broker services; private approvals for cross-destination effects.
- Read-only diagnostics and explicit retry of failed normalization events.
- A pairing helper with memory-only or optional owner-only token-file storage and expanded transport/media live-test entry points.

Validation performed:

- Full offline suite after the client fixes: **200 passed, 1 skipped**; the coordinated final telemetry checks subsequently reported **202 passed, 1 skipped**. The remaining skip is the Linux dedicated-UID canary on macOS. Real macOS sandbox canaries pass, including SQLite startup, protected/sibling file denial, generated-code credential denial and installed Codex startup without inference.
- After poll attribution, line splitting and Unicode draft fixes, the focused Telegram and delivery suite passed **41 tests**.
- Whole-tree Ruff lint/format passed (97 files); strict Pyright reports **0 errors, 0 warnings**.
- The dedicated bot `@theo_fpl0_test_bot` was registered, paired and connected. No Keychain entry was created and its token was not written to repository/configuration files.
- Actual macOS Telegram client checks covered commands, reminders, cancellation/repeated taps, queued edits, typing, a growing synthetic draft, final paragraphs, reply references, document receipt and graceful restart persistence. Extended checks added real photo/audio/video uploads, local transcription and video sampling, approval Details/Approve/Reject with repeated taps, and memory search/history/archive/restore. See [initial evidence](evidence/telegram-client-2026-09-09.json) and [extended evidence](evidence/telegram-client-2026-09-09-extended.json). Native model inference was not exercised.
- Client testing found and fixed empty private drafts before first output and collapsed Rich HTML newlines. Typing now precedes visible text, and explicit HTML breaks preserve paragraph layout.
- The [outbound and Stop evidence](evidence/telegram-client-2026-09-09-outbound.json) records real receipts for every outbound media type in the test pack, the owner selecting a poll answer, a working rendered link, a three-message reply with all text preserved, native Stop cancellation and one incoming photo/video album. Drafts now respect UTF-16 limits for emoji; long final messages prefer complete line/word boundaries. Poll feedback carries its originating action and run when one exists. Media receipt does not establish playback or model understanding.
- A later [recovery check](evidence/telegram-client-2026-09-09-recovery.json) delivered a real message, deliberately lost its acknowledgement before ledger recording, restarted Theo, and confirmed the observed message through an actual Telegram reply. The action kept one send attempt and one receipt after repeated confirmation. This exposed and closed the CLI-only receipt-reconciliation gap. Reply-based confirmation validates bot/chat/topic identity; explicit receipt IDs also reject collisions, incomplete albums and reused chunk receipts. The full suite after this change passed **221 tests, 1 skipped**; 67 focused Telegram/delivery/recovery tests passed, with Ruff and strict Pyright clean.
- The recovery check also exposed missing context when replying to RichMessage-only output. The fix preserves rich references as bounded untrusted evidence and was verified with an actual `/status` reply in Telegram. The final full suite passed **223 tests, 1 skipped**; 46 focused Telegram tests passed, with Ruff and strict Pyright clean.

Remaining external validation:

The recorded [prerequisite recheck](evidence/telegram-client-2026-09-09-prerequisites.json) observed a running daemon, 41 processed updates, zero uncertain deliveries, six jobs waiting for authentication and only the private destination configured. That snapshot does not establish current daemon state. Further live qualification requires account attestation, dedicated group setup and the remaining manual-client operations. Failed automation attempts are recorded as unverified, not as feature passes or product failures.

1. Configure dedicated test groups/topics. During the recorded campaign, automatic approval review rejected group creation because it could not verify the UI target, so no live group result was established.
2. Complete remaining incoming media formats, playback, reaction changes and poll revoting. Native Stop cancelled a synthetic stream through the actual client; cancellation of an active model process remains to be tested. Document work in the recorded setup was waiting for authentication; inspect its current state and retry after account verification as needed.
3. Native Codex login, catalogue and real isolation checks succeeded. Register genuine spending-control evidence for the current runtime and account, then run model-backed live checks; no gates were bypassed.
4. Exercise model/media fixtures, failure/restart drills and the real seven-day service observation.

Current explicit product limits: bot identity changes require an explicit binding migration; group migration is diagnosed for operator review; the rich renderer supports a bounded formatting subset; video coverage is sampled; speech requires local assets. Group-generated shell execution and global account/automation controls remain restricted to the private chat. Arbitrary personal account history, business-account integration, inline/guest mode, Mini Apps and payments are outside scope.

See [Telegram setup and controls](telegram.md), [live testing](live-testing.md), and the [implementation plan](telegram-integration-plan.md).
