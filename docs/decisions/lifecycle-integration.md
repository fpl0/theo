# Lifecycle Integration

**Date:** 2026-03-26
**Ticket:** FPL-18

## Decision

Extend `__main__.py` to orchestrate the full system lifecycle: start all components in dependency order, run until signalled, then shut down in reverse order.

## Key choices

### Testable startup/shutdown functions

Extracted `_startup()` and `_shutdown()` as standalone async functions separate from signal handling in `_run()`. This lets tests verify startup ordering, shutdown ordering, and timeout behaviour without needing to simulate signal delivery or manage event loops.

### Strict config validation at boot

`_validate_config()` checks for `THEO_TELEGRAM_BOT_TOKEN` and `THEO_TELEGRAM_OWNER_CHAT_ID` before any component starts. Missing values produce a single log line listing all absent variables and call `sys.exit(1)`. This fails fast with a clear message instead of a `GateConfigError` stack trace deep in Telegram initialization.

### Startup order

1. Database + migrations (existing)
2. Event bus (components subscribe during their own `start()`)
3. Conversation engine (subscribes to `MessageReceived`)
4. Telegram gate (subscribes to `ResponseChunk`/`ResponseComplete`, starts polling)
5. Health check (warn-only; never blocks startup)

This order ensures each component's dependencies are ready before it starts.

### Shutdown order (reverse of startup)

1. Telegram gate (stop accepting messages)
2. Conversation engine (drain in-flight turns, 30s timeout)
3. Event bus (drain queued events)
4. Database + telemetry

The 30-second drain timeout on the conversation engine prevents a hung LLM call from blocking shutdown indefinitely. If the timeout fires, `kill()` forces the engine to the stopped state.

### Double Ctrl-C force exit

The first SIGINT/SIGTERM sets a stop event that triggers graceful shutdown. A second signal during shutdown calls `os._exit(1)` for immediate termination. This is the standard pattern for CLI tools that perform async cleanup.

### Health check is warn-only

The health check runs after all components start. Warnings are logged for unreachable database or API, but startup continues. This avoids a chicken-and-egg problem where the circuit breaker starts "closed" (API presumed healthy) and the only way to discover API issues is to make an actual call.
