"""Turn execution — context assembly, LLM streaming, and tool loop."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, cast

from opentelemetry import metrics, trace

from theo.bus import bus
from theo.bus.events import ResponseChunk, ResponseComplete
from theo.conversation.context import assemble
from theo.errors import APIUnavailableError, CircuitOpenError
from theo.llm import StreamDone, TextDelta, ToolUseRequest, classify_speed, stream_response
from theo.memory.auto_edges import extract_and_link
from theo.memory.episodes import store_episode
from theo.memory.tools import TOOL_DEFINITIONS, execute_tool
from theo.resilience import circuit_breaker, retry_queue

if TYPE_CHECKING:
    from uuid import UUID

    from anthropic.types import MessageParam, ToolParam

    from theo.bus.events import MessageReceived

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

_turn_duration = _meter.create_histogram(
    "theo.conversation.duration",
    unit="s",
    description="Duration of a full conversation turn",
)
_turn_counter = _meter.create_counter(
    "theo.conversation.turns",
    description="Total conversation turns completed",
)
_tool_call_counter = _meter.create_counter(
    "theo.conversation.tool_calls",
    description="Total tool calls executed during conversation turns",
)

_API_DOWN_ACK = "Got your message \u2014 having trouble reaching Claude. I'll get back to you."

# Background tasks must be referenced to avoid garbage collection (RUF006).
_background_tasks: set[asyncio.Task[None]] = set()

_MAX_TOOL_ITERATIONS = 10


async def execute_turn(  # noqa: C901, PLR0915
    event: MessageReceived,
    session_id: UUID,
    *,
    persist_user_message: bool = True,
) -> None:
    """Run a full conversation turn: context → LLM stream → tool loop → response."""
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
            user_episode_id: int | None = None
            if persist_user_message:
                user_episode_id = await store_episode(
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
                    cast("list[MessageParam]", messages),
                    system=ctx.system_prompt or None,
                    speed=speed,
                    tools=cast("list[ToolParam]", TOOL_DEFINITIONS),
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
                    assistant_content.append({"type": "text", "text": "".join(iteration_chunks)})
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
                    result = await execute_tool(req.name, req.input, episode_id=user_episode_id)
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
            await _handle_api_failure(event, session_id)
            return

        # 5. Persist assistant response as episode.
        full_response = "".join(chunks)
        episode_id = await store_episode(
            session_id=session_id,
            channel=event.channel or "message",
            role="assistant",
            body=full_response,
        )

        # 6. Fire-and-forget: create co-occurrence edges from this session.
        task = asyncio.create_task(
            _safe_extract_and_link(session_id),
            name=f"auto-edge-{session_id}",
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        # 7. Publish completion event.
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


async def _safe_extract_and_link(session_id: UUID) -> None:
    """Run ``extract_and_link`` without propagating errors."""
    try:
        await extract_and_link(session_id)
    except Exception:  # noqa: BLE001
        log.warning(
            "auto-edge extraction failed",
            extra={"session_id": str(session_id)},
            exc_info=True,
        )


async def _handle_api_failure(
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
