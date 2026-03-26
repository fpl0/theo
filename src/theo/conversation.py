"""Conversation engine — orchestrates the request/response cycle.

Subscribes to :class:`MessageReceived` events, assembles context, streams a
response from Claude, persists both sides as episodes, and publishes
:class:`ResponseChunk` / :class:`ResponseComplete` events.

When Claude requests memory tools (store, search, read, update), the engine
executes them and feeds results back in a loop (max 10 iterations).
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
from theo.errors import APIUnavailableError, CircuitOpenError, ConversationNotRunningError
from theo.llm import StreamDone, TextDelta, ToolUseRequest, classify_speed, stream_response
from theo.memory.episodes import store_episode
from theo.memory.tools import TOOL_DEFINITIONS, execute_tool
from theo.resilience import circuit_breaker, retry_queue

if TYPE_CHECKING:
    from uuid import UUID

type EngineState = Literal["running", "paused", "stopped"]

_API_DOWN_ACK = "Got your message \u2014 having trouble reaching Claude. I'll get back to you."

_MAX_TOOL_ITERATIONS = 10

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
_tool_call_counter = meter.create_counter(
    "theo.conversation.tool_calls",
    description="Total tool calls executed during conversation turns",
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
        retry_queue.start(self._retry_message)
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
        await retry_queue.stop()
        # Wait for any in-flight turns to complete.
        await self._drained.wait()
        log.info("conversation engine stopped")

    def kill(self) -> None:
        """Immediately halt without waiting for in-flight turns."""
        self._state = "stopped"
        self._drained.set()
        log.info("conversation engine killed")

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def inflight(self) -> int:
        """Number of turns currently being processed."""
        return self._inflight

    @property
    def queue_depth(self) -> int:
        """Number of messages queued while paused."""
        return self._paused_queue.qsize()

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

    async def _retry_message(
        self,
        *,
        session_id: object,
        channel: str | None,
        body: str,
        trust: str,
    ) -> None:
        """Re-process a previously failed message (called by the retry queue).

        The user message was already persisted as an episode on the original
        attempt, so we skip persistence and go straight to the LLM call.
        """
        from uuid import UUID  # noqa: PLC0415

        sid = session_id if isinstance(session_id, UUID) else UUID(str(session_id))
        event = MessageReceived(
            body=body,
            session_id=sid,
            channel=channel,  # type: ignore[arg-type]
            trust=trust,  # type: ignore[arg-type]
        )
        await self._execute_turn(event, sid, persist_user_message=False)

    async def _execute_turn(  # noqa: C901, PLR0915
        self,
        event: MessageReceived,
        session_id: UUID,
        *,
        persist_user_message: bool = True,
    ) -> None:
        t0 = time.monotonic()
        speed = classify_speed(event.body)
        tool_call_count = 0

        with tracer.start_as_current_span(
            "conversation.turn",
            attributes={
                "session.id": str(session_id),
                "llm.speed": speed,
            },
        ) as span:
            try:
                # 1. Persist incoming message as episode (skip on retry).
                if persist_user_message:
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
                messages: list[dict[str, object]] = [dict(m) for m in ctx.messages]
                messages.append({"role": "user", "content": event.body})

                # 4. Stream response from Claude with tool loop.
                chunks: list[str] = []

                for _iteration in range(_MAX_TOOL_ITERATIONS):
                    tool_requests: list[ToolUseRequest] = []
                    iteration_chunks: list[str] = []

                    raw_stream = stream_response(
                        messages,  # type: ignore[arg-type]
                        system=ctx.system_prompt or None,
                        speed=speed,
                        tools=TOOL_DEFINITIONS,  # type: ignore[arg-type]
                    )
                    async for stream_event in circuit_breaker.call(raw_stream):
                        if isinstance(stream_event, TextDelta):
                            iteration_chunks.append(stream_event.text)
                            await bus.publish(
                                ResponseChunk(
                                    session_id=session_id,
                                    channel=event.channel,
                                    body=stream_event.text,
                                )
                            )
                        elif isinstance(stream_event, ToolUseRequest):
                            tool_requests.append(stream_event)
                        elif isinstance(stream_event, StreamDone):
                            span.set_attribute("llm.model", "resolved")
                            span.set_attribute("llm.input_tokens", stream_event.input_tokens)
                            span.set_attribute("llm.output_tokens", stream_event.output_tokens)

                    chunks.extend(iteration_chunks)

                    if not tool_requests:
                        break

                    # Build assistant message with text + tool_use content blocks.
                    assistant_content: list[dict[str, object]] = []
                    if iteration_chunks:
                        assistant_content.append(
                            {"type": "text", "text": "".join(iteration_chunks)}
                        )
                    tool_blocks: list[dict[str, object]] = [
                        {
                            "type": "tool_use",
                            "id": req.id,
                            "name": req.name,
                            "input": req.input,
                        }
                        for req in tool_requests
                    ]
                    assistant_content.extend(tool_blocks)
                    messages.append({"role": "assistant", "content": assistant_content})

                    # Execute each tool and build tool result messages.
                    tool_results: list[dict[str, object]] = []
                    for req in tool_requests:
                        tool_call_count += 1
                        _tool_call_counter.add(1, {"tool.name": req.name})
                        result = await execute_tool(req.name, req.input)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": req.id,
                                "content": result,
                            }
                        )
                        log.debug(
                            "tool executed",
                            extra={"tool": req.name, "tool_use_id": req.id},
                        )

                    messages.append({"role": "user", "content": tool_results})

                span.set_attribute("turn.tool_calls", tool_call_count)

            except APIUnavailableError, CircuitOpenError:
                await self._handle_api_failure(event, session_id)
                return

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

            # Successful turn — wake the retry queue so queued messages
            # get processed now that the API is reachable again.
            if retry_queue.depth > 0:
                retry_queue.wake()

            log.info(
                "turn complete",
                extra={
                    "session_id": str(session_id),
                    "speed": speed,
                    "duration_s": round(elapsed, 3),
                    "response_length": len(full_response),
                    "tool_calls": tool_call_count,
                },
            )

    async def _handle_api_failure(
        self,
        event: MessageReceived,
        session_id: UUID,
    ) -> None:
        """Acknowledge the user and enqueue the message for retry."""
        log.warning(
            "api unavailable, acknowledging and queuing",
            extra={"session_id": str(session_id)},
        )

        await bus.publish(
            ResponseComplete(
                session_id=session_id,
                channel=event.channel,
                body=_API_DOWN_ACK,
            )
        )

        retry_queue.enqueue(
            session_id=session_id,
            channel=event.channel,
            body=event.body,
            trust=event.trust,
        )
