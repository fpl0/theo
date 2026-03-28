"""Shared stream-and-tool loop used by turn execution and deliberation."""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, cast

from opentelemetry import metrics, trace

from theo.llm import StreamDone, TextDelta, ToolUseRequest, stream_response
from theo.memory.tools import execute_tool
from theo.resilience import circuit_breaker

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from uuid import UUID

    from anthropic.types import MessageParam, ToolParam

    from theo.llm import Speed

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

_tool_call_counter = _meter.create_counter(
    "theo.conversation.tool_calls",
    description="Total tool calls executed during streaming",
)

_MAX_TOOL_ITERATIONS = 10


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class StreamResult:
    """Accumulated result from a stream-and-tool loop."""

    text: str
    input_tokens: int
    output_tokens: int
    tool_call_count: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def stream_and_collect(  # noqa: PLR0913
    messages: list[dict[str, object]],
    *,
    system: str | None,
    speed: Speed,
    tools: list[dict[str, object]] | None = None,
    max_iterations: int = _MAX_TOOL_ITERATIONS,
    on_text: Callable[[str], Awaitable[None]] | None = None,
    episode_id: int | None = None,
    session_id: UUID | None = None,
) -> StreamResult:
    """Stream from Claude with tool-use loop, collecting the full response.

    Parameters
    ----------
    messages:
        Conversation history in Anthropic format (mutated in place).
    system:
        System prompt.
    speed:
        Reasoning tier (selects model).
    tools:
        Tool definitions to pass to the API.
    max_iterations:
        Maximum tool-use rounds before stopping.
    on_text:
        Optional async callback invoked for each text chunk (e.g. to publish
        streaming events). When *None*, text is collected silently.
    episode_id:
        Passed to ``execute_tool`` for cross-referencing stored nodes.
    session_id:
        Passed to ``execute_tool`` for session-scoped tools (e.g. deliberation).

    Returns
    -------
    StreamResult
        Accumulated text, token counts, and tool call count.
    """
    all_chunks: list[str] = []
    total_input = 0
    total_output = 0
    tool_calls = 0

    for _iteration in range(max_iterations):
        chunks, reqs, in_tok, out_tok = await _stream_one(messages, system, speed, tools, on_text)
        all_chunks.extend(chunks)
        total_input += in_tok
        total_output += out_tok

        if not reqs:
            break

        _append_assistant(messages, chunks, reqs)
        tool_calls += await _run_tools(messages, reqs, episode_id, session_id)

    return StreamResult(
        text="".join(all_chunks),
        input_tokens=total_input,
        output_tokens=total_output,
        tool_call_count=tool_calls,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _stream_one(
    messages: list[dict[str, object]],
    system: str | None,
    speed: Speed,
    tools: list[dict[str, object]] | None,
    on_text: Callable[[str], Awaitable[None]] | None,
) -> tuple[list[str], list[ToolUseRequest], int, int]:
    """Run one LLM stream, returning chunks, tool requests, and token counts."""
    chunks: list[str] = []
    tool_requests: list[ToolUseRequest] = []
    in_tok = 0
    out_tok = 0

    raw = stream_response(
        cast("list[MessageParam]", messages),
        system=system,
        speed=speed,
        tools=cast("list[ToolParam]", tools) if tools else None,
    )
    async for event in circuit_breaker.call(raw):
        if isinstance(event, TextDelta):
            chunks.append(event.text)
            if on_text is not None:
                await on_text(event.text)
        elif isinstance(event, ToolUseRequest):
            tool_requests.append(event)
        elif isinstance(event, StreamDone):
            in_tok = event.input_tokens
            out_tok = event.output_tokens

    return chunks, tool_requests, in_tok, out_tok


def _append_assistant(
    messages: list[dict[str, object]],
    chunks: list[str],
    reqs: list[ToolUseRequest],
) -> None:
    """Build and append the assistant message with text + tool_use blocks."""
    content: list[dict[str, object]] = []
    if chunks:
        content.append({"type": "text", "text": "".join(chunks)})
    for r in reqs:
        block: dict[str, object] = {
            "type": "tool_use",
            "id": r.id,
            "name": r.name,
            "input": r.input,
        }
        content.append(block)
    messages.append({"role": "assistant", "content": content})


async def _run_tools(
    messages: list[dict[str, object]],
    reqs: list[ToolUseRequest],
    episode_id: int | None,
    session_id: UUID | None,
) -> int:
    """Execute tool requests, append results, return count."""
    results: list[dict[str, object]] = []
    for req in reqs:
        _tool_call_counter.add(1, {"tool.name": req.name})
        result = await execute_tool(
            req.name,
            req.input,
            episode_id=episode_id,
            session_id=session_id,
        )
        results.append(
            {"type": "tool_result", "tool_use_id": req.id, "content": result},
        )
        log.debug("tool executed", extra={"tool": req.name, "tool_use_id": req.id})
    messages.append({"role": "user", "content": results})
    return len(reqs)
