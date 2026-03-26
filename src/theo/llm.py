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
    RateLimitError,
)
from opentelemetry import metrics, trace

from theo.config import get_settings
from theo.errors import APIUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from anthropic.types import MessageParam, ToolParam

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
_SHORT_THRESHOLD = 30
_LONG_THRESHOLD = 500


def classify_speed(text: str) -> Speed:
    """Classify a user message into a reasoning speed tier."""
    stripped = text.strip()
    if len(stripped) <= _SHORT_THRESHOLD and _REACTIVE_RE.match(stripped):
        return "reactive"
    if _DELIBERATIVE_RE.search(stripped) or len(stripped) > _LONG_THRESHOLD:
        return "deliberative"
    return "reflective"


def _model_for_speed(speed: Speed) -> str:
    cfg = get_settings()
    if speed == "reactive":
        return cfg.llm_model_reactive
    if speed == "deliberative":
        return cfg.llm_model_deliberative
    return cfg.llm_model_reflective


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
    model = _model_for_speed(speed)
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

        except APIConnectionError as exc:
            raise APIUnavailableError(str(exc)) from exc
