# Telegram Gate (FPL-15)

**Date:** 2026-03-26

## Decision: aiogram 3.x over python-telegram-bot

aiogram is async-native (no threading shims), has first-class type annotations, and uses pydantic models internally. It aligns with Theo's async-only, strictly-typed design. python-telegram-bot's async support is bolted on and less ergonomic.

## Decision: Optional config fields with runtime validation

`telegram_bot_token` and `telegram_owner_chat_id` are `None`-defaulted in `Settings` rather than required. This avoids forcing every test and non-Telegram context to provide Telegram credentials. The `TelegramGate` constructor validates their presence at startup and raises `GateConfigError` with a clear message if missing.

## Decision: UUID5 for stable session IDs

`session_id_for_chat(chat_id)` derives a deterministic UUID5 from the Telegram chat ID using a fixed namespace. This means the same chat always maps to the same session, which is essential for context assembly and episode continuity across restarts. UUID5 is preferred over hashing because it produces a proper UUID that fits the existing schema.

## Decision: MarkdownV2 escaping at the gate boundary

Escaping happens in the gate just before sending to Telegram, not in the event payload. Response events carry plain text; the gate is responsible for format conversion. This keeps the bus protocol format-agnostic — if a future gate (email, CLI) subscribes to the same events, it applies its own formatting.

## Decision: Streaming via send-then-edit pattern

The first `ResponseChunk` sends a new Telegram message; subsequent chunks edit that message in-place. `ResponseComplete` finalizes with the full text. This gives the user immediate feedback while avoiding message spam. The `_streaming` dict tracks the in-flight message ID per session.

## Decision: Polling lifecycle as a background task

`start_polling()` is a blocking coroutine, so it runs as a background `asyncio.Task`. `stop()` cancels the task and closes the bot session. This integrates cleanly with Theo's signal-based shutdown pattern without requiring aiogram's internal event system.
