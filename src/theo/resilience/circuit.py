"""Circuit breaker for LLM API calls."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from opentelemetry import trace

from theo.errors import APIUnavailableError, CircuitOpenError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from theo.llm import StreamEvent

type CircuitState = Literal["closed", "open", "half-open"]

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

_CIRCUIT_STATE_VALUES: dict[CircuitState, int] = {
    "closed": 0,
    "open": 1,
    "half-open": 2,
}

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

        with tracer.start_as_current_span(
            "circuit_breaker.call",
            attributes={"circuit.state": current},
        ):
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
