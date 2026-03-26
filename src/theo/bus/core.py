"""EventBus — persistent async pub/sub with replay."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from opentelemetry import metrics, trace

from theo.bus.events import Event
from theo.db import db
from theo.errors import BusNotRunningError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

type Handler[E: Event] = Callable[[E], Awaitable[None]]

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

_published_counter = meter.create_counter(
    "theo.bus.events_published",
    description="Total events published to the bus",
)
_dispatched_counter = meter.create_counter(
    "theo.bus.events_dispatched",
    description="Total events successfully dispatched to all handlers",
)
_handler_errors_counter = meter.create_counter(
    "theo.bus.handler_errors",
    description="Total handler invocation errors",
)

# ── SQL ──────────────────────────────────────────────────────────────

_INSERT_EVENT = """
    INSERT INTO event_queue (id, type, payload, session_id, channel, created_at)
    VALUES ($1, $2, $3::jsonb, $4, $5, $6)
"""

_MARK_PROCESSED = """
    UPDATE event_queue
    SET processed_at = now()
    WHERE id = $1
"""

_REPLAY_UNPROCESSED = """
    SELECT id, type, payload, session_id, channel, created_at
    FROM event_queue
    WHERE processed_at IS NULL
    ORDER BY created_at ASC
    LIMIT 10000
"""


# ── EventBus ─────────────────────────────────────────────────────────


class EventBus:
    """Persistent async pub/sub bus.

    Durable events are written to ``event_queue`` before dispatch.
    On startup, unprocessed rows are replayed to subscribers.
    """

    def __init__(self) -> None:
        self._handlers: dict[type[Event], list[Handler[Event]]] = defaultdict(list)
        self._queue: asyncio.Queue[tuple[Event, bool]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._running = False

    # ── subscribe / publish ──────────────────────────────────────────

    def subscribe[E: Event](
        self,
        event_type: type[E],
        handler: Handler[E],
    ) -> None:
        """Register *handler* for *event_type*. Call before :meth:`start`."""
        self._handlers[event_type].append(handler)  # type: ignore[arg-type]
        handler_name = getattr(handler, "__qualname__", repr(handler))
        log.info(
            "subscribed handler",
            extra={"event_type": event_type.__name__, "handler": handler_name},
        )

    async def publish(self, event: Event) -> None:
        """Persist (if durable) and enqueue *event* for dispatch.

        Raises :class:`BusNotRunningError` if the bus has not been started.
        """
        if not self._running:
            raise BusNotRunningError

        with tracer.start_as_current_span(
            "bus.publish",
            attributes={"event.type": type(event).__name__, "event.durable": event.durable},
        ):
            if event.durable:
                await self._persist(event)

            self._queue.put_nowait((event, event.durable))
            _published_counter.add(1, {"event.type": type(event).__name__})

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Replay unprocessed events, then start the dispatch loop."""
        if self._running:
            return

        self._running = True
        await self._replay()
        self._task = asyncio.create_task(self._dispatch_loop(), name="bus-dispatch")
        log.info("bus started")

    async def stop(self) -> None:
        """Drain queued events and stop the dispatch loop."""
        if not self._running:
            return

        self._running = False

        # Sentinel: signals the loop to exit after draining.
        self._queue.put_nowait((None, False))  # type: ignore[arg-type]

        if self._task is not None:
            await self._task
            self._task = None

        log.info("bus stopped")

    # ── internals ────────────────────────────────────────────────────

    async def _persist(self, event: Event) -> None:
        """Write a durable event to ``event_queue``."""
        payload = event.model_dump_json()
        await db.pool.execute(
            _INSERT_EVENT,
            event.id,
            type(event).__name__,
            payload,
            event.session_id,
            event.channel,
            event.timestamp,
        )

    async def _replay(self) -> None:
        """Re-enqueue unprocessed durable events from the database."""
        with tracer.start_as_current_span("bus.replay"):
            rows = await db.pool.fetch(_REPLAY_UNPROCESSED)
            if not rows:
                log.info("no events to replay")
                return

            from theo.bus import events as events_mod  # noqa: PLC0415

            for row in rows:
                event_cls = getattr(events_mod, row["type"], None)
                if event_cls is None or not (
                    isinstance(event_cls, type) and issubclass(event_cls, Event)
                ):
                    log.warning("unknown event type during replay", extra={"type": row["type"]})
                    continue
                event = event_cls.model_validate_json(row["payload"])
                self._queue.put_nowait((event, True))

            log.info("replayed events", extra={"count": len(rows)})

    async def _dispatch_loop(self) -> None:
        """Pull events from the queue and fan out to handlers."""
        while True:
            item = await self._queue.get()
            event, durable = item

            # Sentinel check — stop() enqueues (None, False).
            if event is None:
                self._queue.task_done()
                break

            event_type_name = type(event).__name__
            with tracer.start_as_current_span(
                "bus.dispatch",
                attributes={"event.type": event_type_name, "event.durable": durable},
            ):
                handlers = self._handlers.get(type(event), [])
                all_ok = True

                for handler in handlers:
                    handler_name = getattr(handler, "__qualname__", repr(handler))
                    try:
                        await handler(event)
                    except Exception:
                        all_ok = False
                        _handler_errors_counter.add(
                            1,
                            {"event.type": event_type_name, "handler": handler_name},
                        )
                        log.exception(
                            "handler failed",
                            extra={
                                "event_type": event_type_name,
                                "handler": handler_name,
                                "event_id": str(event.id),
                            },
                        )

                if all_ok:
                    _dispatched_counter.add(1, {"event.type": event_type_name})

                if durable and all_ok:
                    await db.pool.execute(_MARK_PROCESSED, event.id)

            self._queue.task_done()
