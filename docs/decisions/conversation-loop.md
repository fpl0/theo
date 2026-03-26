# Conversation Loop (FPL-13)

## Decision: Per-session asyncio locks for sequential processing

Messages within a single session are serialized using `asyncio.Lock` keyed by `session_id`. This prevents interleaving of conversation turns within one session (which would corrupt the alternating user/assistant history) while allowing different sessions to process concurrently. The dict of locks grows lazily; cleanup is deferred to M2 since session counts in M1 are trivially small.

## Decision: Three-state lifecycle (running / paused / stopped)

The engine supports three states matching the Telegram control commands planned in FPL-16. **Paused** accepts messages into an internal queue but does not process them — `resume()` drains the queue. **Stopped** rejects messages entirely (raises `ConversationNotRunningError`). This gives the owner graduated control without losing messages during maintenance.

## Decision: Inflight tracking via counter + Event for clean shutdown

`stop()` waits for in-flight turns to complete via an `asyncio.Event` that is set when the inflight counter reaches zero. This avoids the complexity of a semaphore or cancellation while guaranteeing no turn is abandoned mid-stream during shutdown.

## Decision: Subscribe in start(), not in __init__()

The engine registers its `MessageReceived` handler with the bus during `start()`, not at construction time. This means the bus can be started before the engine without messages being routed to an unprepared handler. The tradeoff is that `start()` must be called before the engine can receive messages, which is enforced by the lifecycle.

## Decision: Messages without session_id are dropped

The engine requires a `session_id` on every `MessageReceived` event. Messages without one are logged and silently dropped. This simplifies the entire pipeline — episodes, context assembly, and per-session locking all key on `session_id`. Gates are responsible for assigning session IDs before publishing events.

## Decision: Empty system prompt passed as None

When context assembly produces an empty system prompt (e.g., no core memory seeded yet), it is passed as `None` to `stream_response()` rather than an empty string. The Anthropic API treats `system=""` as valid but wasteful; `None` omits the parameter entirely.
