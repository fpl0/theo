"""Turn execution — context assembly, LLM streaming, and tool loop."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from opentelemetry import metrics, trace

from theo.budget import UsageRecord, check_budget, record_usage
from theo.bus import bus
from theo.bus.events import ResponseChunk, ResponseComplete
from theo.conversation.context import assemble
from theo.conversation.stream import stream_and_collect
from theo.errors import (
    APIUnavailableError,
    BudgetExceededError,
    CircuitOpenError,
    PrivacyViolationError,
)
from theo.llm import Speed, classify_speed, model_for_speed
from theo.memory.auto_edges import extract_and_link
from theo.memory.episodes import store_episode
from theo.memory.tools import TOOL_DEFINITIONS
from theo.resilience import retry_queue

if TYPE_CHECKING:
    from uuid import UUID

    from theo.bus.events import MessageReceived
    from theo.conversation.engine import ConversationEngine

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

_API_DOWN_ACK = "Got your message \u2014 having trouble reaching Claude. I'll get back to you."

# Background tasks must be referenced to avoid garbage collection (RUF006).
_background_tasks: set[asyncio.Task[None]] = set()


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
    engine: ConversationEngine | None = None,
) -> None:
    """Run a full conversation turn: context -> LLM stream -> tool loop -> response."""
    t0 = time.monotonic()
    # Build session context under the lock (caller holds it) to avoid stale snapshots.
    session_context = engine.session_context_for(session_id) if engine is not None else None
    speed, signals = classify_speed(event.body, session_context)
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
            **{f"speed.{k}": str(v) for k, v in signals.items()},
        },
    ) as span:
        try:
            await check_budget(session_id)
            if persist_user_message and not await _persist_incoming(state, span):
                return
            # Record speed only after privacy check passes — rejected messages
            # should not influence the session ratchet.
            if engine is not None:
                engine.record_speed(session_id, speed)
            messages, system_prompt = await _build_initial_messages(state)

            async def _publish_chunk(text: str) -> None:
                await bus.publish(
                    ResponseChunk(
                        session_id=state.session_id,
                        channel=state.event.channel,
                        body=text,
                    )
                )

            async def _before_iteration() -> None:
                await check_budget(state.session_id)

            async def _after_stream(input_tokens: int, output_tokens: int) -> None:
                await record_usage(
                    UsageRecord(
                        session_id=state.session_id,
                        model=state.model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        speed=state.speed,
                    ),
                )

            result = await stream_and_collect(
                messages,
                system=system_prompt,
                speed=state.speed,
                tools=TOOL_DEFINITIONS,
                on_text=_publish_chunk,
                before_iteration=_before_iteration,
                after_stream=_after_stream,
                episode_id=state.user_episode_id,
                session_id=state.session_id,
            )
            state.chunks.extend([result.text])
            state.tool_call_count = result.tool_call_count
            span.set_attribute("llm.input_tokens", result.input_tokens)
            span.set_attribute("llm.output_tokens", result.output_tokens)
        except BudgetExceededError as exc:
            await _handle_budget_exceeded(event, session_id, str(exc))
            return
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
# Phase 3: persist response and publish
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


_BUDGET_EXCEEDED_MSG = (
    "I've reached my token budget for now. I'll be able to help again once the budget resets."
)


async def _handle_budget_exceeded(
    event: MessageReceived,
    session_id: UUID,
    detail: str,
) -> None:
    """Inform the user that the budget has been exceeded."""
    log.warning(
        "budget exceeded, refusing turn",
        extra={"session_id": str(session_id), "detail": detail},
    )
    await bus.publish(
        ResponseComplete(
            session_id=session_id,
            channel=event.channel,
            body=_BUDGET_EXCEEDED_MSG,
        ),
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
