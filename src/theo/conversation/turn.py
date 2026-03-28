"""Turn execution — context assembly, LLM streaming, and tool loop."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from opentelemetry import metrics, trace

from theo.autonomy import classify_tool, log_action, requires_approval
from theo.bus import bus
from theo.bus.events import ResponseChunk, ResponseComplete
from theo.conversation.context import assemble
from theo.errors import APIUnavailableError, CircuitOpenError, PrivacyViolationError
from theo.llm import (
    Speed,
    StreamDone,
    TextDelta,
    ToolUseRequest,
    classify_speed,
    model_for_speed,
    stream_response,
)
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


# ---------------------------------------------------------------------------
# Internal accumulator for the stream + tool loop
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _TurnState:
    """Mutable state accumulated during a single turn."""

    session_id: UUID
    event: MessageReceived
    speed: Speed
    model: str
    user_episode_id: int | None = None
    chunks: list[str] = field(default_factory=list)
    tool_call_count: int = 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def execute_turn(
    event: MessageReceived,
    session_id: UUID,
    *,
    persist_user_message: bool = True,
) -> None:
    """Run a full conversation turn: context -> LLM stream -> tool loop -> response."""
    t0 = time.monotonic()
    speed = classify_speed(event.body)
    model = model_for_speed(speed)
    state = _TurnState(
        session_id=session_id,
        event=event,
        speed=speed,
        model=model,
    )

    with tracer.start_as_current_span(
        "conversation.turn",
        attributes={
            "session.id": str(session_id),
            "llm.speed": speed,
            "llm.model": model,
        },
    ) as span:
        try:
            if persist_user_message and not await _persist_incoming(state, span):
                return
            messages, system_prompt = await _build_initial_messages(state)
            await _stream_and_tool_loop(state, messages, system_prompt, span)
        except APIUnavailableError, CircuitOpenError:
            await _handle_api_failure(event, session_id)
            return

        span.set_attribute("turn.tool_calls", state.tool_call_count)

        await _persist_and_publish(state)

        elapsed = time.monotonic() - t0
        _turn_duration.record(elapsed, {"llm.speed": speed})
        _turn_counter.add(1, {"llm.speed": speed})

        if retry_queue.depth > 0:
            retry_queue.wake()

        log.info(
            "turn complete",
            extra={
                "session_id": str(session_id),
                "speed": speed,
                "duration_s": round(elapsed, 3),
                "response_length": len("".join(state.chunks)),
                "tool_calls": state.tool_call_count,
            },
        )


# ---------------------------------------------------------------------------
# Phase 1: persist incoming message
# ---------------------------------------------------------------------------


async def _persist_incoming(state: _TurnState, span: trace.Span) -> bool:
    """Store the user message as an episode. Returns False if rejected."""
    try:
        state.user_episode_id = await store_episode(
            session_id=state.session_id,
            channel=state.event.channel or "message",
            role="user",
            body=state.event.body,
            trust=state.event.trust,
        )
    except PrivacyViolationError:
        log.warning(
            "privacy filter rejected user message episode",
            extra={"session_id": str(state.session_id), "trust": state.event.trust},
        )
        span.set_attribute("turn.privacy_rejected", "true")
        return False
    return True


# ---------------------------------------------------------------------------
# Phase 2: assemble context and build initial messages
# ---------------------------------------------------------------------------


async def _build_initial_messages(
    state: _TurnState,
) -> tuple[list[dict[str, object]], str | None]:
    """Assemble context window and build the starting message list."""
    ctx = await assemble(
        session_id=state.session_id,
        latest_message=state.event.body,
    )
    messages: list[dict[str, object]] = [dict(m) for m in ctx.messages]
    messages.append({"role": "user", "content": state.event.body})
    return messages, ctx.system_prompt or None


# ---------------------------------------------------------------------------
# Phase 3: stream + tool loop
# ---------------------------------------------------------------------------


async def _stream_and_tool_loop(
    state: _TurnState,
    messages: list[dict[str, object]],
    system_prompt: str | None,
    span: trace.Span,
) -> None:
    """Stream from Claude, execute tools, loop until done or max iterations."""
    for _iteration in range(_MAX_TOOL_ITERATIONS):
        iteration_chunks, tool_requests = await _stream_one_iteration(
            state,
            messages,
            system_prompt,
            span,
        )
        state.chunks.extend(iteration_chunks)

        if not tool_requests:
            break

        _append_assistant_message(messages, iteration_chunks, tool_requests)
        await _execute_tools(state, messages, tool_requests)


async def _stream_one_iteration(
    state: _TurnState,
    messages: list[dict[str, object]],
    system_prompt: str | None,
    span: trace.Span,
) -> tuple[list[str], list[ToolUseRequest]]:
    """Run one LLM stream, collecting text chunks and tool requests."""
    tool_requests: list[ToolUseRequest] = []
    iteration_chunks: list[str] = []

    raw_stream = stream_response(
        cast("list[MessageParam]", messages),
        system=system_prompt,
        speed=state.speed,
        tools=cast("list[ToolParam]", TOOL_DEFINITIONS),
    )
    async for stream_event in circuit_breaker.call(raw_stream):
        if isinstance(stream_event, TextDelta):
            iteration_chunks.append(stream_event.text)
            await bus.publish(
                ResponseChunk(
                    session_id=state.session_id,
                    channel=state.event.channel,
                    body=stream_event.text,
                )
            )
        elif isinstance(stream_event, ToolUseRequest):
            tool_requests.append(stream_event)
        elif isinstance(stream_event, StreamDone):
            span.set_attribute("llm.input_tokens", stream_event.input_tokens)
            span.set_attribute("llm.output_tokens", stream_event.output_tokens)

    return iteration_chunks, tool_requests


def _append_assistant_message(
    messages: list[dict[str, object]],
    iteration_chunks: list[str],
    tool_requests: list[ToolUseRequest],
) -> None:
    """Build and append the assistant message with text + tool_use blocks."""
    assistant_content: list[dict[str, object]] = []
    if iteration_chunks:
        assistant_content.append({"type": "text", "text": "".join(iteration_chunks)})
    for req in tool_requests:
        tool_block: dict[str, object] = {
            "type": "tool_use",
            "id": req.id,
            "name": req.name,
            "input": req.input,
        }
        assistant_content.append(tool_block)
    messages.append({"role": "assistant", "content": assistant_content})


async def _execute_tools(
    state: _TurnState,
    messages: list[dict[str, object]],
    tool_requests: list[ToolUseRequest],
) -> None:
    """Execute each tool request, classify autonomy, and append results."""
    tool_results: list[dict[str, object]] = []
    for req in tool_requests:
        state.tool_call_count += 1
        _tool_call_counter.add(1, {"tool.name": req.name})

        classification = classify_tool(req.name)

        # Propose/consult actions will route to approval gateway (FPL-37).
        # For now all actions execute and get logged.
        if requires_approval(classification.autonomy_level):
            log.info(
                "action requires approval (executing pending gateway)",
                extra={
                    "tool": req.name,
                    "autonomy_level": classification.autonomy_level,
                    "action_type": classification.action_type,
                },
            )

        result = await execute_tool(req.name, req.input, episode_id=state.user_episode_id)

        await log_action(
            classification.action_type,
            classification.autonomy_level,
            "executed",
            context={"tool": req.name, "tool_use_id": req.id},
            session_id=state.session_id,
        )

        tool_results.append(
            {"type": "tool_result", "tool_use_id": req.id, "content": result},
        )
        log.debug("tool executed", extra={"tool": req.name, "tool_use_id": req.id})
    messages.append({"role": "user", "content": tool_results})


# ---------------------------------------------------------------------------
# Phase 4: persist response and publish
# ---------------------------------------------------------------------------


async def _persist_and_publish(state: _TurnState) -> None:
    """Persist the assistant response, extract edges, publish completion."""
    full_response = "".join(state.chunks)

    try:
        episode_id = await store_episode(
            session_id=state.session_id,
            channel=state.event.channel or "message",
            role="assistant",
            body=full_response,
        )
    except PrivacyViolationError:
        log.warning(
            "privacy filter rejected assistant episode",
            extra={"session_id": str(state.session_id)},
        )
        episode_id = None

    task = asyncio.create_task(
        _safe_extract_and_link(state.session_id),
        name=f"auto-edge-{state.session_id}",
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    await bus.publish(
        ResponseComplete(
            session_id=state.session_id,
            channel=state.event.channel,
            body=full_response,
            episode_id=episode_id,
        )
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
