# Graceful degradation

**Ticket:** FPL-17
**Date:** 2026-03-26

## Context

Theo must handle Anthropic API failures gracefully: messages are never lost, and the user always gets feedback within seconds. This requires a circuit breaker to detect and react to sustained failures, and a retry queue to re-process messages when the API recovers.

## Decisions

### Circuit breaker is a dataclass, not a class hierarchy

A single `CircuitBreaker` dataclass covers all three states (closed, open, half-open) via a `_state` field and time-based transitions. No State pattern or abstract base class. The state machine is simple enough that conditionals in `call()` are clearer than polymorphism.

### Circuit breaker wraps async generators, not functions

The circuit breaker's `call()` method accepts and yields from an `AsyncGenerator[StreamEvent]`. This preserves streaming semantics: the conversation engine can still process text deltas as they arrive while the circuit breaker tracks success/failure at the stream boundary. The alternative (wrapping the entire turn) would lose streaming granularity.

### Half-open uses a lock, not a counter

Only one test request is allowed while half-open. An `asyncio.Lock` ensures this naturally. Concurrent callers during half-open get `CircuitOpenError` immediately rather than queuing.

### Retry queue is in-memory, not database-backed

Messages are already persisted as episodes before the LLM call, so durability is guaranteed by the existing event bus. The retry queue only tracks what needs re-processing. An in-memory list with a background drain loop is sufficient. If Theo crashes, the event bus replay mechanism will catch unprocessed messages on restart.

### Health check uses circuit state as API reachability proxy

Instead of making a real API call (which costs tokens and adds latency), the health check reports `api_reachable` based on the circuit breaker state: closed = reachable, open/half-open = unreachable. This is accurate enough for operational monitoring and avoids unnecessary API calls.

### Module-level singletons with test isolation

`circuit_breaker` and `retry_queue` are module-level singletons in `theo.resilience`, consistent with Theo's pattern for `db`, `bus`, and `embedder`. Tests reset the circuit breaker directly and replace the retry queue on the `resilience` module while patching the imported references in `theo.conversation.turn` and `theo.conversation.engine`.

### Retry queue wakeup via explicit signal

The retry queue's drain loop sleeps until woken by `wake()`. Two triggers: (1) new items enqueued, and (2) successful conversation turns (indicating the API is back). This avoids polling and unnecessary retries.

## OTEL instrumentation

- **`theo.resilience.circuit_state`** (observable gauge): 0=closed, 1=open, 2=half-open. Uses async callback so it always reflects current state.
- **`theo.resilience.queue_depth`** (up-down counter): tracks retry queue size via increment/decrement on enqueue/dequeue.

## Files changed

- `src/theo/errors.py` — added `CircuitOpenError`
- `src/theo/resilience/` — package: `circuit.py` (CircuitBreaker), `retry.py` (RetryQueue), `health.py` (HealthStatus, health_check), `__init__.py` (singletons, OTEL gauge)
- `src/theo/conversation/engine.py` — integrated circuit breaker around `stream_response`, API failure handling with acknowledgment and retry enqueue
- `src/theo/conversation/turn.py` — retry queue integration for failed turns
- `tests/test_resilience.py` — 24 tests covering circuit transitions, queue FIFO, health check, and conversation engine integration
- `tests/test_conversation.py` — test isolation fixture for resilience singletons
