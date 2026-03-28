"""Anthropic LLM client with streaming and tool-use support."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from anthropic import (
    APIConnectionError,
    APITimeoutError,
    AsyncAnthropic,
    InternalServerError,
    RateLimitError,
)
from opentelemetry import metrics, trace

from theo.config import get_settings
from theo.errors import APIUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from anthropic.types import MessageParam, ToolParam

    from theo.config import Settings

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

_duration = _meter.create_histogram(
    "theo.llm.duration",
    unit="s",
    description="LLM API call duration",
)

# ── Types ────────────────────────────────────────────────────────────

type Speed = Literal["reactive", "reflective", "deliberative"]

_SPEED_ORDER: dict[Speed, int] = {"reactive": 0, "reflective": 1, "deliberative": 2}


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Per-session state for context-aware speed classification."""

    peak_speed: Speed | None = None
    prior_speeds: tuple[Speed, ...] = ()
    has_active_deliberation: bool = False


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A chunk of streamed text."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolUseRequest:
    """Claude is requesting a tool call."""

    id: str
    name: str
    input: dict[str, object]


@dataclass(frozen=True, slots=True)
class StreamDone:
    """End-of-stream signal with usage stats."""

    input_tokens: int
    output_tokens: int
    stop_reason: str


type StreamEvent = TextDelta | ToolUseRequest | StreamDone

# ── Speed classification ─────────────────────────────────────────────

_REACTIVE_RE = re.compile(
    r"^(hi|hey|hello|yo|sup|thanks|thank you|ok|okay|sure|yep|yeah|nah|no|yes|bye|gn|gm)"
    r"\s*[!.?]*$",
    re.IGNORECASE,
)
_DELIBERATIVE_RE = re.compile(
    r"\b(think|research|analyze|analyse|compare|evaluate|investigate|plan|design|review)\b",
    re.IGNORECASE,
)
_TASK_INDICATOR_RE = re.compile(
    r"\b(step[- ]by[- ]step|first\b.*\bthen\b|how should|what if|pros and cons|trade.?offs"
    r"|prioriti[sz]e|recommend|strategy|outline|break.?down)\b",
    re.IGNORECASE,
)
_SHORT_THRESHOLD = 30
_LONG_THRESHOLD = 500


def _classify_base(stripped: str) -> tuple[Speed, str]:
    """Message-level heuristic classification."""
    if len(stripped) <= _SHORT_THRESHOLD and _REACTIVE_RE.match(stripped):
        return "reactive", "greeting_or_ack"
    if _DELIBERATIVE_RE.search(stripped):
        return "deliberative", "reasoning_keyword"
    if len(stripped) > _LONG_THRESHOLD:
        return "deliberative", "long_message"
    if _TASK_INDICATOR_RE.search(stripped):
        return "deliberative", "task_indicator"
    return "reflective", "default"


def _apply_session_context(
    base: Speed,
    ctx: SessionContext,
    signals: dict[str, object],
) -> Speed:
    """Promote speed based on session history and active deliberation."""
    effective = base

    # History bias: if recent turns were mostly deliberative, promote
    # reflective to deliberative (don't promote reactive — that's an ack).
    if (
        ctx.prior_speeds
        and effective == "reflective"
        and sum(1 for s in ctx.prior_speeds if s == "deliberative") > len(ctx.prior_speeds) // 2
    ):
        effective = "deliberative"
        signals["history_promoted"] = True

    # Active deliberation forces deliberative minimum.
    if ctx.has_active_deliberation and _SPEED_ORDER[effective] < _SPEED_ORDER["deliberative"]:
        effective = "deliberative"
        signals["active_deliberation_promoted"] = True

    return effective


def _apply_ratchet(
    base: Speed,
    effective: Speed,
    ctx: SessionContext,
    signals: dict[str, object],
) -> Speed:
    """Hold speed at session peak unless a downgrade signal is detected."""
    cfg = get_settings()
    if not cfg.session_ratchet_enabled or ctx.peak_speed is None:
        reason = "history_bias" if signals.get("history_promoted") else "base"
        signals["final_reason"] = reason
        return effective

    peak_ord = _SPEED_ORDER[ctx.peak_speed]
    eff_ord = _SPEED_ORDER[effective]

    if eff_ord >= peak_ord:
        signals["final_reason"] = "classified_above_peak"
        return effective

    # Reactive ack after deliberative is a downgrade signal.
    if base == "reactive" and ctx.peak_speed == "deliberative":
        signals["ratchet_downgrade"] = True
        signals["final_reason"] = "downgrade_signal"
        return effective

    signals["ratchet_held"] = True
    signals["final_reason"] = "ratchet"
    return ctx.peak_speed


def classify_speed(
    text: str,
    session_context: SessionContext | None = None,
) -> tuple[Speed, dict[str, object]]:
    """Classify a user message into a reasoning speed tier.

    Returns the classified speed and a dict of signals that contributed to
    the decision (for observability).
    """
    stripped = text.strip()
    base, base_reason = _classify_base(stripped)
    signals: dict[str, object] = {"base_speed": base, "base_reason": base_reason}

    if session_context is None:
        signals["final_reason"] = "no_session_context"
        return base, signals

    effective = _apply_session_context(base, session_context, signals)
    effective = _apply_ratchet(base, effective, session_context, signals)
    signals["effective_speed"] = effective
    return effective, signals


def model_for_speed(speed: Speed, cfg: Settings | None = None) -> str:
    """Return the model ID for a given speed tier."""
    resolved = cfg or get_settings()
    if speed == "reactive":
        return resolved.llm_model_reactive
    if speed == "deliberative":
        return resolved.llm_model_deliberative
    return resolved.llm_model_reflective


# ── Streaming ────────────────────────────────────────────────────────

_MAX_RATE_LIMIT_RETRIES = 3
_MAX_TIMEOUT_RETRIES = 1


async def stream_response(  # noqa: C901
    messages: list[MessageParam],
    *,
    system: str | None = None,
    speed: Speed = "reflective",
    tools: list[ToolParam] | None = None,
    max_tokens: int | None = None,
) -> AsyncGenerator[StreamEvent]:
    """Stream a response from the Anthropic API.

    Yields ``TextDelta`` events as text arrives, ``ToolUseRequest`` when Claude
    calls a tool, and a final ``StreamDone`` with token counts.
    """
    cfg = get_settings()
    model = model_for_speed(speed, cfg)
    resolved_max = max_tokens or cfg.llm_max_tokens

    client = AsyncAnthropic(
        api_key=cfg.anthropic_api_key.get_secret_value(),
        max_retries=0,
    )

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": resolved_max,
        "messages": messages,
    }
    if system is not None:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools

    rate_limit_attempts = 0
    timeout_attempts = 0

    try:
        while True:
            t0 = time.monotonic()
            try:
                with tracer.start_as_current_span(
                    "llm.stream",
                    attributes={"llm.model": model, "llm.speed": speed},
                ) as span:
                    async with client.messages.stream(**kwargs) as stream:
                        async for event in stream:
                            if event.type == "text":
                                yield TextDelta(text=event.text)
                            elif (
                                event.type == "content_block_stop"
                                and event.content_block.type == "tool_use"
                            ):
                                yield ToolUseRequest(
                                    id=event.content_block.id,
                                    name=event.content_block.name,
                                    input=event.content_block.input,
                                )

                        final = await stream.get_final_message()
                        elapsed = time.monotonic() - t0

                        in_tok = final.usage.input_tokens
                        out_tok = final.usage.output_tokens
                        span.set_attribute("llm.input_tokens", in_tok)
                        span.set_attribute("llm.output_tokens", out_tok)
                        _duration.record(elapsed, {"llm.model": model, "llm.speed": speed})

                        log.info(
                            "llm stream complete",
                            extra={
                                "model": model,
                                "speed": speed,
                                "input_tokens": in_tok,
                                "output_tokens": out_tok,
                                "duration_s": round(elapsed, 3),
                            },
                        )

                        yield StreamDone(
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                            stop_reason=final.stop_reason or "unknown",
                        )
                        return

            except RateLimitError:
                rate_limit_attempts += 1
                if rate_limit_attempts > _MAX_RATE_LIMIT_RETRIES:
                    raise
                delay = 2**rate_limit_attempts
                log.warning(
                    "rate limited, retrying",
                    extra={"delay_s": delay, "attempt": rate_limit_attempts},
                )
                await asyncio.sleep(delay)

            except APITimeoutError:
                timeout_attempts += 1
                if timeout_attempts > _MAX_TIMEOUT_RETRIES:
                    raise
                log.warning("timeout, retrying", extra={"attempt": timeout_attempts})

            except (APIConnectionError, InternalServerError) as exc:
                raise APIUnavailableError(str(exc)) from exc
    finally:
        await client.close()
