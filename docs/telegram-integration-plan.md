# Telegram integration design plan — 8 September 2026

Historical design and acceptance criteria. The implementation and partial client checks are now recorded in [implementation status](telegram-implementation.md); the [Telegram guide](telegram.md) describes current usage. The foundation, suggested decomposition and source paths below describe the pre-implementation checkout, not the current package layout. See [source organization](architecture.md#source-organization) for current paths.
Prepared: 8 September 2026.
Baseline: remote HEAD `2664e1433746cb716b96b5f17bcc2bd45346690a` plus the current local working tree, including ongoing runtime and delivery changes.

## Outcome and scope

Make Telegram a complete daily interface to Theo: converse naturally, exchange media, review actions and memory changes, manage work, receive reminders, and recover from failures without routine CLI intervention.

Confirmed scope: the owner's private chat plus explicitly allowed groups and topics, selected by the owner on 8 September 2026. Group participation must have a separate context and memory disclosure policy. This remains a personal assistant with one privileged owner; allowing a group does not authorize its members to control Theo or access private owner memory.

Complete means every capability in the matrix below has implementation, failure behavior, tests, and live evidence. It does not mean wrapping every Bot API method. Business/Secretary account access, arbitrary personal account history, inline/guest mode, Mini Apps, payments, gifts, community administration, and multi-owner hosting are separate product extensions outside the confirmed scope.

## Capability and completion matrix

| Capability | Existing foundation | Required completion |
|---|---|---|
| Text and rich content | Plain text in/out; Unicode chunking | Structured formatting, safe links, rich input normalization, readable long answers and fallback |
| Replies and quotes | Outgoing reply parameter | Incoming reply/quote context, canonical remote identities, default response threading |
| Edits | Edits treated as new input | Versioned messages, correction semantics, stale-run fencing |
| Live interaction | Durable final/progress tools | Typing, draft streaming, stop/cancel, bounded updates and reconnect behavior |
| Media | Core file types, extraction, transcription hooks | Albums, additional common types, extraction statuses, video sampling, round-trip validation |
| Controls | Slash commands returning mostly JSON | Discoverable command menu, paginated views, buttons, human-readable state |
| Approvals | Action ledger and callback parser | Review cards, approve/reject/detail flow, expiry and replay handling |
| Memory review | CLI/domain primitives | Proposed correction/fact review, history, archive and restore from Telegram |
| Feedback | Owner reaction observations | Reaction removal/counts, message attribution, polls and answer changes |
| Destinations | One configured chat | Explicit bindings for permitted chats/topics and scoped disclosure |
| Reminders and jobs | Durable scheduler and jobs | In-chat create/list/change/cancel, timezone previews, progress and completion controls |
| Operations | Polling, outbox, supervisor alerts | Setup diagnostics, degraded state, recovery UI, delivery backlog visibility |
| Evidence | Offline tests; four-case live runner | Live feature matrix, restart/failure drills, service soak |

## Architectural decisions

1. Keep SQLite, Jobs, Delivery, and the broker authoritative. Telegram is an adapter and interaction layer, never an independent memory or job system.
2. Separate durable inbound receipt from normalization and execution. Acknowledgement follows committed receipt; interrupted normalization resumes from stored input. Bound retries and expose failed events so one problematic update cannot wedge polling.
3. Preserve raw accepted updates with an explicit retention policy. Normalize only supported fields into trusted structures; message text, filenames, forwarded content, and rich markup remain untrusted content.
4. Model remote identity explicitly. Never confuse Telegram message IDs, update IDs, Theo message IDs, and outbox receipt IDs.
5. Distinguish ephemeral presentation from committed effects. Drafts/typing can expire; final sends and meaningful mutations remain ledger-controlled. A displayed preview is not a delivery receipt or proof of task completion.
6. Centralize destination policy, authorization, rendering, and capability checks. Keep tools and normal assistant replies on the same implementation path.
7. Keep long polling as the default for the existing desktop daemon. Webhook hosting is an optional transport project, not a prerequisite for feature completeness.

Suggested adapter decomposition: keep a compatibility entry point in `channels.py`, with focused modules for Telegram transport, normalization, routing, rendering, interactions, and previews. Extract modules as they acquire responsibilities; avoid a wholesale runtime rewrite.

## Phase 0 — Lock the contract and compatibility

Work:

- Record supported chat surfaces, inbound update types, outbound operations, and feature-specific permissions in a checked-in capability matrix.
- Verify the locked aiogram 3.31.0 request/update models against the current Bot API. Upgrade only for demonstrated missing support and run the existing adapter contracts afterward.
- Establish typed destination and message-reference contracts before changing schemas or tools.
- Preserve current private-chat behavior and capture existing database/configuration migration fixtures.
- Separate Telegram completion from unrelated native-provider and production qualification gates.

Acceptance: every matrix item has a scope decision and a named live scenario; no unsupported feature is silently presented as available. The implementation branch includes or rebases onto the current local runtime work intentionally.

## Phase 1 — Durable message identity and conversation semantics

Primary files: `storage.py`, a new checksummed SQL migration, `channels.py`, `jobs.py`, `context.py`, `domain.py`, `delivery.py`.

Work:

- Add destination bindings keyed by bot identity, chat, and optional topic; retain the existing conversation IDs when migrating the configured private chat.
- Add remote-message mappings and immutable revisions. Store inbound/outbound direction, canonical message ID, Telegram message ID, reply/quote references, media-group identity, and latest edit metadata. Unique keys must include the chat and bot identity.
- Track receipt/normalization state separately from update deduplication. Unknown updates become recorded unsupported events, not empty model prompts.
- Normalize replies, quotes, forwarded provenance, entities, and rich content into bounded context. If the referenced message is unavailable, explicitly mark that gap.
- Handle edits according to job state: replace queued input using the latest revision; revoke/fence an active stale run and queue one replacement; after completion, retain the historical answer and create a correction turn only for meaningful content changes. Already executed effects remain recorded and are never implicitly undone.
- Persist album accumulation with a short collection window and a bounded maximum wait. Deduplicate members, retain ordering, recover across restart, and treat late arrivals as explicit follow-ups.
- Ensure polls, callbacks, and reactions resolve destinations through their own identifiers rather than assuming every update contains `message.from_user`.

Acceptance: duplicate/reordered updates, repeated edits, reply chains, unknown references, late album items, and restart during normalization preserve one coherent history and cannot replay completed effects. Existing terminal conversation behavior passes regression checks.

## Phase 2 — Complete the private-chat interaction loop

Primary files: Telegram rendering/interactions modules, `runtime.py`, `tools.py`, `delivery.py`, `operations.py`.

Work:

- Register commands and provide readable `/start`, `/help`, `/status`, `/jobs`, `/models`, `/backend`, `/memory`, `/goals`, `/usage`, `/pause`, and `/resume` views with pagination.
- Add host-owned callback routing for job details/cancel, model selection, schedules, approvals, and memory/fact review. Persist callback state with expiry and bind it to actor, conversation, object revision, and allowed operation.
- Present approval cards containing the exact destination, action, content/media preview, and expiry. Support approve, reject, inspect, and refresh. Distinguish pending, approved, executing, delivered, rejected, expired, and uncertain.
- Reuse domain services for action approval, memory corrections, fact proposals, archive/restore, and schedule changes. Factor CLI-only logic into shared services where necessary.
- Add a recovery view for uncertain actions. Show available receipts and require an explicit assertion of no effect before retrying a send whose acceptance is unknown. Never imply the bot can automatically inspect arbitrary Telegram history.
- Acknowledge button taps promptly, then render the durable outcome. Repeated taps must be harmless; expired or inaccessible cards must produce an understandable response.
- Route ordinary answers to the initiating message when that reference remains available; define visible fallback when a reply target has disappeared.

Acceptance: after operator setup, the owner can complete an approval, reject it, review a correction, switch models, inspect/cancel work, and manage a reminder entirely inside Telegram. None of these UI actions requires a model to authorize itself.

## Phase 3 — Streaming, formatting, and cancellation

Primary files: `runtime.py`, Telegram previews/rendering modules, `delivery.py`, `tools.py`.

Work:

- Use native private-chat drafts where the verified API/client combination supports them; use bounded typing/status updates elsewhere.
- Forward only visible answer deltas. Never expose hidden reasoning, credentials, raw tool arguments, or unreviewed external-destination content.
- Give each run/generation a stable preview identity. Coalesce updates, bound buffers, and apply rate limits independently from the final outbox.
- Connect Telegram stop events and host Cancel buttons to existing grant revocation and process cancellation. Stale generations cannot continue previews or finalize.
- Create structured rendering for paragraphs, emphasis, code, quotations, links, lists, and supported rich blocks. Validate model-produced structure and fall back to literal text when needed.
- Finalize through the action ledger once; expire/clear previews. Support long answers and caption overflow with reference-preserving chunks.
- Explicitly classify a known formatting rejection before attempting a plain-text fallback; never retry an ambiguously accepted send as a fallback.

Acceptance: a long answer streams visibly and ends with one logical final delivery; cancel and restart do not produce stale finals. Unicode, malformed markup, long code blocks, rate limits, and unsupported clients retain a readable result. Preview failure cannot block durable completion.

## Phase 4 — Media and feedback completeness

Primary files: `channels.py` or successor modules, `artifacts.py`, `media.py`, `tools.py`, `context.py`, `delivery.py`.

Work:

- Finish round trips for photos, documents, voice, audio, video, and location; add media groups, animation, stickers, video notes, contacts, and venues where relevant to the scope matrix.
- Preserve original bytes and provenance. Add explicit downloading, extracting, ready, failed, unsupported, and oversized states, with independently bounded extraction work.
- Validate MIME/content agreement, duration, dimensions, file sizes, and extraction budgets. Keep Telegram transport limits distinct from configurable local processing limits.
- Provision and verify speech prerequisites through the existing setup path. Provide transcript/voice reply preferences and a textual fallback.
- Replace first-frame-only video handling with bounded timestamped samples and an audio transcript where supported. Report sampled coverage; do not promise exhaustive video understanding.
- Cache reusable Telegram file references scoped to the bot, with fallback to the stored artifact after a known invalid-reference response.
- Track poll IDs, observed answers and answer changes, completion, and anonymity. Track individual reaction changes/removals separately from anonymous counts.
- Bind feedback to the correct delivered message/run; absence of an event is not negative feedback or complete observation.

Acceptance: real fixtures for every supported media type succeed in both directions, including captions and albums. Missing assets and oversized/corrupt inputs produce useful responses. Poll/reaction updates never grant authority or silently become permanent owner preferences.

## Phase 5 — Allowed groups and topics

Dependency: Phase 1 destination contracts. Groups and topics are included in the confirmed scope.

Primary files: `config.py`, routing/policy modules, `storage.py`, `context.py`, `memory.py`, `tools.py`, `scheduling.py`.

Work:

- Add operator-controlled chat/topic bindings and explicit owner-only invocation rules. Start with commands, owner mentions, and owner replies to Theo; avoid responding to every group message.
- Partition conversations by destination/topic and route replies, reminders, approvals, and media to the originating destination.
- Establish visibility labels for private owner memory and group-scoped evidence. Group runs cannot retrieve private memory by default; sharing requires a specific reviewed action. Enforce this in retrieval and tool services, not just prompts.
- Treat other participants' messages as untrusted contextual evidence, never owner instructions. Unidentifiable or anonymous actors cannot perform privileged actions.
- Add capability/permission diagnostics, chat migration handling, removed/blocked bot states, and closed/deleted topic handling.
- Distinguish viewing a status in a group from authorizing an action; sensitive review details go to the owner private chat with an explicit destination binding.

Acceptance: two topics and the private chat retain independent context and routing. An unauthorized member cannot cancel work, approve an action, change settings, or retrieve private facts through direct prompts, quotes, or button taps.

## Phase 6 — Operations and live qualification

Primary files: `cli.py`, `operations.py`, `supervisor.py`, `scripts/telegram_e2e.py`, `tests/`, and operating documentation.

Work:

- Add setup/doctor checks for bot identity, configured destinations, available permissions, command registration, webhook/polling conflicts, media assets, and transport reachability. Never print token-bearing URLs.
- Classify 429, permanent request rejection, revoked credentials, blocked bot, transient transport failure, and ambiguous acceptance. Preserve receipts and bound retries by operation semantics.
- Show polling lag, normalization backlog, oldest pending delivery, failed media processing, and unresolved actions in diagnostics.
- Keep independent supervisor alerts and test them with a dedicated test destination.
- Expand the live runner into transport-only, model-backed, recovery, and manual-client suites. Adapt its observer for message revisions, albums, previews, and legitimate retries; preserve checks against duplicate successful effects.
- Use a dedicated test bot, private chat, allowed test group/topics, synthetic facts and files. Record backend/model versions, commit and working-tree state, request outcomes, receipts, and redacted reports.
- Test migration/backup/restore against pre-change data; preserve pending obligations and approvals. Roll back binaries only when the schema is compatible; otherwise restore the verified backup into quarantine.

Release gates:

1. Required offline tests, Ruff, and strict type checks pass, with no unexplained skips in Telegram behavior.
2. Live text/memory/reply/edit/media/approval/poll/reaction/reminder/control scenarios pass using the real daemon and at least one qualified native backend; validate other advertised backends separately.
3. Restart drills before acknowledgement, during processing, during preview, and after possible send acceptance preserve the declared durability semantics.
4. Manual Telegram-client checks verify formatting, previews, buttons, albums, and cancellation that the user-client test library cannot fully inspect.
5. A real seven-day service observation completes for production qualification; simulated time does not count. Report lost input, duplicate effects, approval violations, latency, downtime, and unresolved delivery states.
6. Update acceptance and compatibility documents with the actual evidence and any remaining feature-specific limitations.

## Suggested implementation slices and dependencies

| Slice | Reviewable result | Depends on |
|---|---|---|
| 1 | Capability contracts, migration, remote message identities | Phase 0 |
| 2 | Resumable normalization, replies, edits, albums | 1 |
| 3 | In-chat controls, approvals, review/recovery flows | 1–2 |
| 4 | Draft streaming, formatting, stop/cancel | 1–3 |
| 5 | Media completion, polls, reaction attribution | 1–2; integrates with 4 |
| 6 | Destination bindings, group memory boundaries, topic routing | 1–3 |
| 7 | Operational diagnostics and expanded live runner | Incrementally alongside 2–6 |
| 8 | Recorded live results, client checks, migration/restart drills, soak | All included capabilities |

Each slice includes its own behavioral tests and documentation changes. Do not postpone failure handling and evidence collection until the final slice. Estimate effort after Slice 1 establishes compatibility and schema changes; calendar promises before that would be speculative.

## Sources and platform constraints

Verified against the [Telegram Bot API](https://core.telegram.org/bots/api) on 8 September 2026 (page identifies Bot API 10.3). Private-chat [message drafts](https://core.telegram.org/bots/api#sendmessagedraft) and [rich drafts](https://core.telegram.org/bots/api#sendrichmessagedraft) are temporary previews and require a separate final send. [Update delivery](https://core.telegram.org/bots/api#getting-updates) has finite server retention; Theo must not promise recovery of arbitrarily old unreceived messages. [Anonymous reaction counts](https://core.telegram.org/bots/api#messagereactioncountupdated) are not attributable owner decisions.

Local reference points: `src/theo/channels.py`, `runtime.py`, `jobs.py`, `delivery.py`, `tools.py`, `context.py`, `migrations/`, `scripts/telegram_e2e.py`, and `docs/live-testing.md`. Existing focused checks observed during planning: 21 passed; these are offline evidence, not live Telegram qualification.
