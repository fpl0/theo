"""Tests for theo.conversation.stream — shared stream-and-tool loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from theo.conversation.stream import StreamResult, stream_and_collect
from theo.llm import StreamDone, TextDelta, ToolUseRequest
from theo.resilience import CircuitBreaker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_circuit_breaker():
    """Provide a fresh circuit breaker per test."""
    with patch("theo.conversation.stream.circuit_breaker", CircuitBreaker()):
        yield


# ---------------------------------------------------------------------------
# Basic streaming
# ---------------------------------------------------------------------------


class TestStreamAndCollect:
    async def test_collects_text_chunks(self) -> None:
        async def fake_stream(_messages, **kwargs):  # noqa: ARG001
            yield TextDelta(text="Hello")
            yield TextDelta(text=" world")
            yield StreamDone(input_tokens=10, output_tokens=5, stop_reason="end_turn")

        with patch("theo.conversation.stream.stream_response", fake_stream):
            result = await stream_and_collect(
                [{"role": "user", "content": "hi"}],
                system=None,
                speed="reflective",
            )

        assert result.text == "Hello world"
        assert result.input_tokens == 10
        assert result.output_tokens == 5
        assert result.tool_call_count == 0

    async def test_invokes_on_text_callback(self) -> None:
        chunks: list[str] = []

        async def on_text(text: str) -> None:
            chunks.append(text)

        async def fake_stream(_messages, **kwargs):  # noqa: ARG001
            yield TextDelta(text="a")
            yield TextDelta(text="b")
            yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="end_turn")

        with patch("theo.conversation.stream.stream_response", fake_stream):
            await stream_and_collect(
                [{"role": "user", "content": "hi"}],
                system=None,
                speed="reflective",
                on_text=on_text,
            )

        assert chunks == ["a", "b"]

    async def test_silent_when_no_on_text(self) -> None:
        """No error when on_text is None."""

        async def fake_stream(_messages, **kwargs):  # noqa: ARG001
            yield TextDelta(text="x")
            yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="end_turn")

        with patch("theo.conversation.stream.stream_response", fake_stream):
            result = await stream_and_collect(
                [{"role": "user", "content": "hi"}],
                system=None,
                speed="reflective",
            )

        assert result.text == "x"


# ---------------------------------------------------------------------------
# Tool loop
# ---------------------------------------------------------------------------


class TestToolLoop:
    async def test_executes_tool_and_continues(self) -> None:
        call_count = 0

        async def fake_stream(_messages, **kwargs):  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield TextDelta(text="thinking...")
                yield ToolUseRequest(id="t1", name="search_memory", input={"query": "test"})
                yield StreamDone(input_tokens=5, output_tokens=3, stop_reason="tool_use")
            else:
                yield TextDelta(text="answer")
                yield StreamDone(input_tokens=5, output_tokens=3, stop_reason="end_turn")

        with (
            patch("theo.conversation.stream.stream_response", fake_stream),
            patch(
                "theo.conversation.stream.execute_tool",
                new_callable=AsyncMock,
                return_value='{"results": []}',
            ) as mock_tool,
        ):
            result = await stream_and_collect(
                [{"role": "user", "content": "test"}],
                system=None,
                speed="reflective",
                tools=[{"name": "search_memory"}],
            )

        assert result.text == "thinking...answer"
        assert result.tool_call_count == 1
        assert result.input_tokens == 10
        assert result.output_tokens == 6
        mock_tool.assert_awaited_once_with(
            "search_memory",
            {"query": "test"},
            episode_id=None,
            session_id=None,
        )

    async def test_respects_max_iterations(self) -> None:
        """Tool loop stops after max_iterations even if tools keep coming."""

        async def always_tool(_messages, **kwargs):  # noqa: ARG001
            yield ToolUseRequest(id="t1", name="search_memory", input={"query": "x"})
            yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="tool_use")

        with (
            patch("theo.conversation.stream.stream_response", always_tool),
            patch(
                "theo.conversation.stream.execute_tool",
                new_callable=AsyncMock,
                return_value="{}",
            ),
        ):
            result = await stream_and_collect(
                [{"role": "user", "content": "hi"}],
                system=None,
                speed="reflective",
                tools=[{"name": "search_memory"}],
                max_iterations=3,
            )

        assert result.tool_call_count == 3

    async def test_passes_episode_and_session_ids(self) -> None:
        from uuid import uuid4

        sid = uuid4()

        async def fake_stream(_messages, **kwargs):  # noqa: ARG001
            yield ToolUseRequest(id="t1", name="store_memory", input={"body": "x"})
            yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="tool_use")

        with (
            patch("theo.conversation.stream.stream_response", fake_stream),
            patch(
                "theo.conversation.stream.execute_tool",
                new_callable=AsyncMock,
                return_value="{}",
            ) as mock_tool,
        ):
            await stream_and_collect(
                [{"role": "user", "content": "hi"}],
                system=None,
                speed="reflective",
                tools=[{"name": "store_memory"}],
                episode_id=42,
                session_id=sid,
                max_iterations=1,
            )

        mock_tool.assert_awaited_once_with(
            "store_memory",
            {"body": "x"},
            episode_id=42,
            session_id=sid,
        )


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


class TestStreamResult:
    def test_frozen(self) -> None:
        r = StreamResult(text="x", input_tokens=1, output_tokens=1, tool_call_count=0)
        with pytest.raises(AttributeError):
            r.text = "y"  # type: ignore[misc]  # ty: ignore[invalid-assignment]

    def test_has_slots(self) -> None:
        r = StreamResult(text="x", input_tokens=1, output_tokens=1, tool_call_count=0)
        assert hasattr(r, "__slots__")
        assert not hasattr(r, "__dict__")
