"""Graceful degradation — circuit breaker, retry queue, and health check.

The circuit breaker wraps LLM API calls and trips after consecutive failures,
giving the upstream time to recover.  While the circuit is open, incoming
messages are acknowledged to the user and queued for retry once the API is
healthy again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from opentelemetry import metrics, trace

from theo.errors import APIUnavailableError, CircuitOpenError, DatabaseNotConnectedError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from theo.llm import StreamEvent

type CircuitState = Literal["closed", "open", "half-open"]

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

# ── Metrics ───────────────────────────────────────────────────────────

_CIRCUIT_STATE_VALUES: dict[CircuitState, int] = {
    "closed": 0,
    "open": 1,
    "half-open": 2,
}

_queue_depth = _meter.create_up_down_counter(
    "theo.resilience.queue_depth",
    description="Current depth of the retry queue",
)

# ── Circuit breaker ───────────────────────────────────────────────────

_FAILURE_THRESHOLD = 3
_OPEN_TIMEOUT_S = 30.0


@dataclass
class CircuitBreaker:
    """Three-state circuit breaker for LLM API calls.

    * **closed** — normal operation, calls pass through.
    * **open** — calls rejected immediately for *open_timeout_s* seconds.
    * **half-open** — one test call allowed; success → close, failure → re-open.
    """

    failure_threshold: int = _FAILURE_THRESHOLD
    open_timeout_s: float = _OPEN_TIMEOUT_S

    _state: CircuitState = field(default="closed", init=False)
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _half_open_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    @property
    def state(self) -> CircuitState:
        if self._state == "open" and self._timeout_elapsed():
            return "half-open"
        return self._state

    @property
    def state_value(self) -> int:
        """Numeric representation for the OTEL gauge callback."""
        return _CIRCUIT_STATE_VALUES[self.state]

    def _timeout_elapsed(self) -> bool:
        return (time.monotonic() - self._opened_at) >= self.open_timeout_s

    def _transition(self, new: CircuitState) -> None:
        old = self._state
        self._state = new
        if new == "open":
            self._opened_at = time.monotonic()
        log.info(
            "circuit state transition",
            extra={"from": old, "to": new},
        )

    async def call(
        self,
        stream: AsyncGenerator[StreamEvent],
    ) -> AsyncGenerator[StreamEvent]:
        """Wrap an async generator with circuit-breaker protection.

        Raises :class:`CircuitOpenError` when the circuit is open.
        On half-open, only one concurrent test call is allowed.
        """
        current = self.state

        if current == "open":
            raise CircuitOpenError

        if current == "half-open":
            if self._half_open_lock.locked():
                raise CircuitOpenError
            async with self._half_open_lock:
                async for event in self._guarded(stream):
                    yield event
                return

        async for event in self._guarded(stream):
            yield event

    async def _guarded(
        self,
        stream: AsyncGenerator[StreamEvent],
    ) -> AsyncGenerator[StreamEvent]:
        """Consume the underlying stream, tracking success / failure."""
        try:
            async for event in stream:
                yield event
            self._on_success()
        except APIUnavailableError:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        self._consecutive_failures = 0
        if self._state != "closed":
            self._transition("closed")

    def _on_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._transition("open")

    def reset(self) -> None:
        """Reset to closed state (used in tests and shutdown)."""
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at = 0.0


# ── Retry queue ───────────────────────────────────────────────────────


class RetryProcessor(Protocol):
    """Callable that re-processes a queued message."""

    async def __call__(
        self,
        *,
        session_id: object,
        channel: str | None,
        body: str,
        trust: str,
    ) -> None: ...


@dataclass
class _RetryItem:
    session_id: object
    channel: str | None
    body: str
    trust: str
    enqueued_at: float = field(default_factory=time.monotonic)


class RetryQueue:
    """FIFO queue for messages that failed due to API unavailability.

    Messages are already persisted as episodes before reaching this queue,
    so durability is guaranteed.  The queue only tracks what needs to be
    re-processed once the API recovers.
    """

    def __init__(self) -> None:
        self._items: list[_RetryItem] = []
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._wakeup = asyncio.Event()
        self._process_fn: RetryProcessor | None = None

    @property
    def depth(self) -> int:
        return len(self._items)

    def enqueue(
        self,
        *,
        session_id: object,
        channel: str | None,
        body: str,
        trust: str,
    ) -> None:
        """Add a message to the retry queue."""
        self._items.append(
            _RetryItem(
                session_id=session_id,
                channel=channel,
                body=body,
                trust=trust,
            )
        )
        _queue_depth.add(1)
        log.info(
            "enqueued message for retry",
            extra={"session_id": str(session_id), "queue_depth": self.depth},
        )
        self._wakeup.set()

    def start(self, process_fn: RetryProcessor) -> None:
        """Start the background drain loop."""
        if self._running:
            return
        self._running = True
        self._process_fn = process_fn
        self._task = asyncio.create_task(self._drain_loop(), name="retry-drain")
        if self._items:
            self._wakeup.set()
        log.info("retry queue started")

    async def stop(self) -> None:
        """Stop the drain loop. Queued items remain for next start."""
        if not self._running:
            return
        self._running = False
        self._wakeup.set()
        if self._task is not None:
            await self._task
            self._task = None
        log.info("retry queue stopped", extra={"remaining": self.depth})

    def wake(self) -> None:
        """Signal the drain loop to attempt processing."""
        self._wakeup.set()

    async def _drain_loop(self) -> None:
        """Process queued items whenever signalled."""
        while self._running:
            if self._items:
                drained = await self._process_pending()
                if drained:
                    continue
            # Wait for a wake signal (new items or recovery).
            self._wakeup.clear()
            # Re-check running after clear to avoid missing stop signal.
            if not self._running:
                break
            await self._wakeup.wait()

    async def _process_pending(self) -> bool:
        """Try to process all pending items in FIFO order.

        Returns ``True`` if the queue was fully drained, ``False`` if
        processing stopped due to an API failure.
        """
        assert self._process_fn is not None  # noqa: S101
        while self._items and self._running:
            item = self._items[0]
            with tracer.start_as_current_span(
                "resilience.retry",
                attributes={"session.id": str(item.session_id)},
            ):
                try:
                    await self._process_fn(
                        session_id=item.session_id,
                        channel=item.channel,
                        body=item.body,
                        trust=item.trust,
                    )
                    self._items.pop(0)
                    _queue_depth.add(-1)
                    log.info(
                        "retried message successfully",
                        extra={
                            "session_id": str(item.session_id),
                            "queue_depth": self.depth,
                        },
                    )
                except (APIUnavailableError, CircuitOpenError):
                    log.warning(
                        "retry failed, will try again later",
                        extra={"queue_depth": self.depth},
                    )
                    return False
        return True


# ── Health check ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Structured health report."""

    db_connected: bool
    api_reachable: bool
    telegram_connected: bool
    circuit_state: CircuitState
    retry_queue_depth: int


async def health_check(
    *,
    circuit: CircuitBreaker,
    queue: RetryQueue,
) -> HealthStatus:
    """Return a point-in-time health snapshot."""
    with tracer.start_as_current_span("resilience.health_check"):
        db_ok = await _check_db()
        api_ok = circuit.state == "closed"
        tg_ok = _check_telegram()

        status = HealthStatus(
            db_connected=db_ok,
            api_reachable=api_ok,
            telegram_connected=tg_ok,
            circuit_state=circuit.state,
            retry_queue_depth=queue.depth,
        )

        log.info(
            "health check",
            extra={
                "db_connected": status.db_connected,
                "api_reachable": status.api_reachable,
                "telegram_connected": status.telegram_connected,
                "circuit_state": status.circuit_state,
                "retry_queue_depth": status.retry_queue_depth,
            },
        )

        return status


async def _check_db() -> bool:
    from theo.db import db  # noqa: PLC0415

    try:
        await db.pool.execute("SELECT 1")
    except DatabaseNotConnectedError:
        return False
    else:
        return True


def _check_telegram() -> bool:
    from theo.config import get_settings  # noqa: PLC0415

    cfg = get_settings()
    return cfg.telegram_bot_token is not None


# ── Module singletons ─────────────────────────────────────────────────

circuit_breaker = CircuitBreaker()
retry_queue = RetryQueue()


def _observe_circuit_state() -> int:
    return circuit_breaker.state_value


_meter.create_observable_gauge(
    "theo.resilience.circuit_state",
    callbacks=[lambda _options: [metrics.Observation(value=_observe_circuit_state())]],
    description="Circuit breaker state: 0=closed, 1=open, 2=half-open",
)
