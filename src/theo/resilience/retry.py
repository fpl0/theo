"""Retry queue for messages that failed due to API unavailability."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from opentelemetry import metrics, trace

from theo.errors import APIUnavailableError, CircuitOpenError

if TYPE_CHECKING:
    from uuid import UUID

    from theo.bus.events import Channel, TrustTier

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

_queue_depth = _meter.create_up_down_counter(
    "theo.resilience.queue_depth",
    description="Current depth of the retry queue",
)


class RetryProcessor(Protocol):
    """Callable that re-processes a queued message."""

    async def __call__(
        self,
        *,
        session_id: UUID,
        channel: Channel | None,
        body: str,
        trust: TrustTier,
    ) -> None: ...


@dataclass
class _RetryItem:
    session_id: UUID
    channel: Channel | None
    body: str
    trust: TrustTier
    enqueued_at: float = field(default_factory=time.monotonic)


class RetryQueue:
    """FIFO queue for messages that failed due to API unavailability.

    Messages are already persisted as episodes before reaching this queue,
    so durability is guaranteed.  The queue only tracks what needs to be
    re-processed once the API recovers.
    """

    def __init__(self) -> None:
        self._items: deque[_RetryItem] = deque()
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
        session_id: UUID,
        channel: Channel | None,
        body: str,
        trust: TrustTier,
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
                    self._items.popleft()
                    _queue_depth.add(-1)
                    log.info(
                        "retried message successfully",
                        extra={
                            "session_id": str(item.session_id),
                            "queue_depth": self.depth,
                        },
                    )
                except APIUnavailableError, CircuitOpenError:
                    log.warning(
                        "retry failed, will try again later",
                        extra={"queue_depth": self.depth},
                    )
                    return False
        return True
