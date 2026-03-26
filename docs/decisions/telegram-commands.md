# Telegram Commands and System Control

**Ticket**: FPL-16
**Date**: 2026-03-26

## Decision

Add slash command handlers to the Telegram gate for owner-controlled system management: `/start`, `/pause`, `/resume`, `/stop`, `/kill`, `/status`.

## Key choices

### Engine reference via constructor injection

The `TelegramGate` accepts an optional `ConversationEngine` parameter. This keeps the coupling explicit and testable without introducing a module-level singleton for the engine (that responsibility belongs to FPL-18 lifecycle integration). Commands degrade gracefully when no engine is provided.

### Command registration order

aiogram 3.x dispatches to the first matching handler in registration order. Command handlers (with `Command` filters) are registered before the catch-all `_on_message` handler. This ensures `/pause` is handled by `_on_cmd_pause`, not published as a `MessageReceived` event.

### kill() vs stop() semantics

- `stop()` sets state to "stopped" and awaits the `_drained` event, allowing in-flight turns to complete.
- `kill()` sets state to "stopped" and immediately sets `_drained`, so no waiting occurs. In-flight turns may still complete naturally, but no new work starts and any awaiting `stop()` call unblocks.

### Plain text responses

Command responses use `parse_mode=None` to bypass the bot's default MarkdownV2 parsing. Status messages are simple plaintext; escaping them would add complexity for no visual benefit.

### Status reporting

`/status` reports what's available from the engine and gate: engine state, in-flight count, queue depth, and uptime. API health and last-message-time are deferred to FPL-17 (graceful degradation) which introduces the health check infrastructure.
