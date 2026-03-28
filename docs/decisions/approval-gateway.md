# Approval Gateway via Telegram (FPL-37)

**Date:** 2026-03-28

## Context

Actions classified as `propose` or `consult` by the autonomy system (FPL-33) need owner approval before execution. This is the UI layer that presents proposals and collects responses via Telegram's inline keyboard feature.

## Decisions

### Plain text with ALL-CAPS labels, not MarkdownV2

Proposals use `parse_mode=None`. MarkdownV2 escaping of user-authored content in proposals is a maintenance nightmare — the summary and detail come from Theo's reasoning, which may contain special characters. ALL-CAPS labels (WHAT, WHY) provide visual anchors that scan well on mobile without any escaping issues.

### Inline keyboard with three actions in one row

Three buttons (`Approve`, `Modify`, `Reject`) in a single row. This is the minimal complete set — approve (proceed), reject (cancel), modify (adjust). A two-button design (approve/reject) lacks nuance for cases where the idea is right but details need tweaking. More buttons would crowd the mobile UI.

### Callback data format: `proposal:<uuid-short>:<action>`

First 8 hex chars of the proposal UUID as the short ID. This keeps callback data well under Telegram's 64-byte limit while providing sufficient uniqueness for the small number of concurrent proposals (max 5).

### Separate ProposalGateway module, not inline in TelegramGate

`telegram.py` was already 550 lines. The proposal handling — state management, callbacks, timeouts, modify-via-reply — adds significant complexity. Extracting to `gates/proposals.py` keeps both modules focused per CLAUDE.md convention (~200 lines per module). The gateway receives the bot and dispatcher at construction, registers its callback handler, and is composed into TelegramGate.

### Modify via native Telegram reply, not inline text input

When the owner taps "Modify", they're prompted to reply to the proposal message. Telegram's reply feature provides natural UX — quote the proposal, type the modification. The alternative (inline keyboard text input) doesn't exist in Telegram's bot API. The `_awaiting_modification` dict maps message_id to short-id so we can recognize which proposal a reply targets. Non-reply messages fall through to normal processing — no stuck state.

### No reminders, auto-expire with configurable timeouts

Reminders feel like nagging. The owner knows the proposal is there — Telegram badges unread messages. Auto-expiry with different defaults per autonomy tier (propose: 4h, consult: 24h) prevents stale proposals from accumulating. On expiry, the keyboard is removed and a `ProposalExpired` event is published.

### Concurrent proposal cap with backpressure notification

Up to 5 concurrent proposals (configurable). If exceeded, the owner is notified to clear pending proposals. This is a backpressure signal, not a silent drop — the owner knows why a proposal wasn't presented. The cap prevents Theo from flooding the chat with proposals.

### ProposalExpired is ephemeral, Created/Response are durable

`ProposalCreated` and `ProposalResponse` are durable — they represent important state transitions that should survive a restart (the bus replays unprocessed events). `ProposalExpired` is ephemeral — it's a notification, and the timeout will naturally re-fire on restart if the proposal is still pending.

### Keyboard removal via edit_message_reply_markup, not text editing

On approve/reject/expire, we remove the inline keyboard by setting `reply_markup=None`. We don't attempt to edit the proposal text (which would require storing it or re-fetching it from Telegram). This is simpler and sufficient — the callback answer toast already tells the owner what happened.

## Files changed

| File | Change |
|------|--------|
| `src/theo/gates/proposals.py` | New module: ProposalGateway with inline keyboard, callbacks, timeouts, modify-via-reply |
| `src/theo/gates/telegram.py` | Integrates ProposalGateway — creates in init, subscribes in start, hooks reply check in message handler |
| `src/theo/gates/__init__.py` | Exports ProposalGateway |
| `src/theo/bus/events.py` | New events: ProposalCreated, ProposalResponse, ProposalExpired |
| `src/theo/config.py` | New settings: proposal_timeout_propose_s, proposal_timeout_consult_s, max_pending_proposals |
| `tests/test_proposals.py` | Tests covering all response paths, timeout, modify-via-reply, concurrent cap, integration |
| `docs/decisions/approval-gateway.md` | This document |
