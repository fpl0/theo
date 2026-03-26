"""Conversation engine — orchestrates the request/response cycle.

Subscribes to :class:`MessageReceived` events, assembles context, streams a
response from Claude, persists both sides as episodes, and publishes
:class:`ResponseChunk` / :class:`ResponseComplete` events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Literal

from opentelemetry import metrics, trace

from theo.bus import bus
from theo.bus.events import MessageReceived, ResponseChunk, ResponseComplete
from theo.context import assemble
from theo.errors import ConversationNotRunningError
from theo.llm import StreamDone, TextDelta, classify_speed, stream_response
from theo.memory.episodes import store_episode

if TYPE_CHECKING:
    from uuid import UUID

type EngineState = Literal["running", "paused", "stopped"]

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

_turn_duration = meter.create_histogram(
    "theo.conversation.duration",
    unit="s",
    description="Duration of a full conversation turn",
)
_turn_counter = meter.create_counter(
    "theo.conversation.turns",
    description="Total conversation turns completed",
)


class ConversationEngine:
    """Processes incoming messages through the LLM pipeline.

    Lifecycle: ``start`` → ``pause``/``resume`` → ``stop``.

    Concurrency: one turn at a time per session. Messages arriving while a
    session is busy are queued and processed in order.
    """

    def __init__(self) -> None:
        self._state: EngineState = "stopped"
        # Per-session lock ensures sequential processing within a session.
        self._session_locks: dict[UUID, asyncio.Lock] = {}
        # Internal queue for messages received while paused.
        self._paused_queue: asyncio.Queue[MessageReceived] = asyncio.Queue()
        # Track in-flight turns for clean shutdown drain.
        self._inflight = 0
        self._drained = asyncio.Event()
        self._drained.set()  # starts drained (no work in progress)

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the engine and subscribe to the event bus."""
        if self._state == "running":
            return
        self._state = "running"
        bus.subscribe(MessageReceived, self._on_message)
        log.info("conversation engine started")

    def pause(self) -> None:
        """Pause processing — messages are queued but not processed."""
        if self._state != "running":
            return
        self._state = "paused"
        log.info("conversation engine paused")

    async def resume(self) -> None:
        """Resume processing and drain any queued messages."""
        if self._state != "paused":
            return
        self._state = "running"
        log.info("conversation engine resumed")
        await self._drain_paused_queue()

    async def stop(self) -> None:
        """Stop accepting messages and wait for in-flight turns to finish."""
        if self._state == "stopped":
            return
        self._state = "stopped"
        # Wait for any in-flight turns to complete.
        await self._drained.wait()
        log.info("conversation engine stopped")

    @property
    def state(self) -> EngineState:
        return self._state

    # ── event handler ─────────────────────────────────────────────────

    async def _on_message(self, event: MessageReceived) -> None:
        """Bus handler for MessageReceived events."""
        if self._state == "stopped":
            raise ConversationNotRunningError
        if self._state == "paused":
            self._paused_queue.put_nowait(event)
            log.debug("queued message while paused", extra={"event_id": str(event.id)})
            return
        await self._process_message(event)

    async def _drain_paused_queue(self) -> None:
        """Process all messages that arrived while paused."""
        while not self._paused_queue.empty():
            event = self._paused_queue.get_nowait()
            await self._process_message(event)

    # ── core turn logic ───────────────────────────────────────────────

    async def _process_message(self, event: MessageReceived) -> None:
        """Execute a full conversation turn for one message."""
        session_id = event.session_id
        if session_id is None:
            log.warning("dropping message without session_id", extra={"event_id": str(event.id)})
            return

        lock = self._session_locks.setdefault(session_id, asyncio.Lock())

        async with lock:
            self._inflight += 1
            self._drained.clear()
            try:
                await self._execute_turn(event, session_id)
            finally:
                self._inflight -= 1
                if self._inflight == 0:
                    self._drained.set()

        # Clean up session lock if no longer in use.
        lock = self._session_locks.get(session_id)
        if lock is not None and not lock.locked():
            self._session_locks.pop(session_id, None)

    async def _execute_turn(self, event: MessageReceived, session_id: UUID) -> None:
        t0 = time.monotonic()
        speed = classify_speed(event.body)

        with tracer.start_as_current_span(
            "conversation.turn",
            attributes={
                "session.id": str(session_id),
                "llm.speed": speed,
            },
        ) as span:
            # 1. Persist incoming message as episode.
            await store_episode(
                session_id=session_id,
                channel=event.channel or "message",
                role="user",
                body=event.body,
                trust=event.trust,
            )

            # 2. Assemble context window.
            ctx = await assemble(
                session_id=session_id,
                latest_message=event.body,
            )

            # 3. Build message list: history + current message.
            messages = [*ctx.messages, {"role": "user", "content": event.body}]

            # 4. Stream response from Claude.
            chunks: list[str] = []
            async for stream_event in stream_response(
                messages,  # type: ignore[arg-type]
                system=ctx.system_prompt or None,
                speed=speed,
            ):
                if isinstance(stream_event, TextDelta):
                    chunks.append(stream_event.text)
                    await bus.publish(
                        ResponseChunk(
                            session_id=session_id,
                            channel=event.channel,
                            body=stream_event.text,
                        )
                    )
                elif isinstance(stream_event, StreamDone):
                    span.set_attribute("llm.model", "resolved")
                    span.set_attribute("llm.input_tokens", stream_event.input_tokens)
                    span.set_attribute("llm.output_tokens", stream_event.output_tokens)

            # 5. Persist assistant response as episode.
            full_response = "".join(chunks)
            episode_id = await store_episode(
                session_id=session_id,
                channel=event.channel or "message",
                role="assistant",
                body=full_response,
            )

            # 6. Publish completion event.
            await bus.publish(
                ResponseComplete(
                    session_id=session_id,
                    channel=event.channel,
                    body=full_response,
                    episode_id=episode_id,
                )
            )

            elapsed = time.monotonic() - t0
            _turn_duration.record(elapsed, {"llm.speed": speed})
            _turn_counter.add(1, {"llm.speed": speed})

            log.info(
                "turn complete",
                extra={
                    "session_id": str(session_id),
                    "speed": speed,
                    "duration_s": round(elapsed, 3),
                    "response_length": len(full_response),
                },
            )
