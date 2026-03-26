"""Tests for the Anthropic LLM client (theo.llm)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anthropic import APIConnectionError, APITimeoutError, RateLimitError

from theo.config import Settings
from theo.errors import APIUnavailableError
from theo.llm import (
    StreamDone,
    TextDelta,
    ToolUseRequest,
    classify_speed,
    stream_response,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


# ── Speed classification ─────────────────────────────────────────────


class TestClassifySpeed:
    def test_greeting_is_reactive(self) -> None:
        assert classify_speed("hello") == "reactive"

    def test_greeting_with_punctuation_is_reactive(self) -> None:
        assert classify_speed("Hi!") == "reactive"

    def test_thanks_is_reactive(self) -> None:
        assert classify_speed("thanks") == "reactive"

    def test_ok_is_reactive(self) -> None:
        assert classify_speed("ok") == "reactive"

    def test_bye_is_reactive(self) -> None:
        assert classify_speed("bye") == "reactive"

    def test_short_question_is_reflective(self) -> None:
        assert classify_speed("what's the weather?") == "reflective"

    def test_normal_request_is_reflective(self) -> None:
        assert classify_speed("tell me about dogs") == "reflective"

    def test_research_keyword_is_deliberative(self) -> None:
        assert (
            classify_speed("research the best flights to Lisbon and compare options")
            == "deliberative"
        )

    def test_analyze_keyword_is_deliberative(self) -> None:
        assert classify_speed("analyze this data set") == "deliberative"

    def test_think_keyword_is_deliberative(self) -> None:
        assert classify_speed("think about this problem") == "deliberative"

    def test_long_message_is_deliberative(self) -> None:
        assert classify_speed("a" * 501) == "deliberative"

    def test_whitespace_stripped(self) -> None:
        assert classify_speed("  hello  ") == "reactive"

    def test_case_insensitive_greeting(self) -> None:
        assert classify_speed("HELLO") == "reactive"

    def test_case_insensitive_deliberative(self) -> None:
        assert classify_speed("COMPARE these two") == "deliberative"


# ── Streaming helpers ────────────────────────────────────────────────


def _make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "database_url": "postgresql://u:p@h:5432/d",
        "anthropic_api_key": "sk-ant-test-key",
    }
    return Settings(**{**defaults, **overrides}, _env_file=None)


def _make_text_event(text: str) -> MagicMock:
    ev = MagicMock()
    ev.type = "text"
    ev.text = text
    return ev


def _make_tool_stop_event(tool_id: str, name: str, tool_input: dict[str, object]) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = tool_input

    ev = MagicMock()
    ev.type = "content_block_stop"
    ev.content_block = block
    return ev


def _make_text_stop_event() -> MagicMock:
    block = MagicMock()
    block.type = "text"

    ev = MagicMock()
    ev.type = "content_block_stop"
    ev.content_block = block
    return ev


def _make_final_message(
    *,
    input_tokens: int = 10,
    output_tokens: int = 20,
    stop_reason: str = "end_turn",
) -> MagicMock:
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

    msg = MagicMock()
    msg.usage = usage
    msg.stop_reason = stop_reason
    return msg


class _FakeStream:
    """Mock for the async context manager returned by client.messages.stream()."""

    def __init__(
        self,
        events: Sequence[MagicMock],
        final: MagicMock,
    ) -> None:
        self._events = events
        self._final = final

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    def __aiter__(self) -> Self:
        self._iter = iter(self._events)
        return self

    async def __anext__(self) -> MagicMock:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None

    async def get_final_message(self) -> MagicMock:
        return self._final


async def _collect(gen: Any) -> list[Any]:
    return [event async for event in gen]


# ── Stream response tests ────────────────────────────────────────────


class TestStreamResponse:
    @pytest.fixture(autouse=True)
    def _patch_settings(self) -> Any:
        with patch("theo.llm.get_settings", return_value=_make_settings()):
            yield

    async def test_yields_text_deltas(self) -> None:
        events = [_make_text_event("Hello"), _make_text_event(" world")]
        final = _make_final_message()
        fake_stream = _FakeStream(events, final)

        with patch("theo.llm.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.stream = MagicMock(return_value=fake_stream)
            result = await _collect(
                stream_response([{"role": "user", "content": "hi"}], speed="reflective")
            )

        assert result[0] == TextDelta(text="Hello")
        assert result[1] == TextDelta(text=" world")
        assert isinstance(result[2], StreamDone)

    async def test_yields_tool_use_request(self) -> None:
        tool_input: dict[str, object] = {"location": "Lisbon"}
        events = [
            _make_text_event("Let me check"),
            _make_tool_stop_event("tool_1", "get_weather", tool_input),
        ]
        final = _make_final_message(stop_reason="tool_use")
        fake_stream = _FakeStream(events, final)

        with patch("theo.llm.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.stream = MagicMock(return_value=fake_stream)
            result = await _collect(
                stream_response(
                    [{"role": "user", "content": "weather?"}],
                    speed="reflective",
                    tools=[
                        {
                            "name": "get_weather",
                            "description": "Get weather",
                            "input_schema": {
                                "type": "object",
                                "properties": {"location": {"type": "string"}},
                            },
                        }
                    ],
                )
            )

        text_deltas = [e for e in result if isinstance(e, TextDelta)]
        tool_requests = [e for e in result if isinstance(e, ToolUseRequest)]
        done_events = [e for e in result if isinstance(e, StreamDone)]

        assert len(text_deltas) == 1
        assert text_deltas[0].text == "Let me check"
        assert len(tool_requests) == 1
        assert tool_requests[0].name == "get_weather"
        assert tool_requests[0].input == {"location": "Lisbon"}
        assert len(done_events) == 1
        assert done_events[0].stop_reason == "tool_use"

    async def test_stream_done_has_token_counts(self) -> None:
        events = [_make_text_event("hi")]
        final = _make_final_message(input_tokens=15, output_tokens=25)
        fake_stream = _FakeStream(events, final)

        with patch("theo.llm.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.stream = MagicMock(return_value=fake_stream)
            result = await _collect(
                stream_response([{"role": "user", "content": "hi"}], speed="reactive")
            )

        done = result[-1]
        assert isinstance(done, StreamDone)
        assert done.input_tokens == 15
        assert done.output_tokens == 25

    async def test_ignores_text_content_block_stop(self) -> None:
        events = [_make_text_event("hi"), _make_text_stop_event()]
        final = _make_final_message()
        fake_stream = _FakeStream(events, final)

        with patch("theo.llm.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.stream = MagicMock(return_value=fake_stream)
            result = await _collect(
                stream_response([{"role": "user", "content": "hi"}], speed="reflective")
            )

        assert len(result) == 2
        assert isinstance(result[0], TextDelta)
        assert isinstance(result[1], StreamDone)

    async def test_passes_system_prompt(self) -> None:
        events = [_make_text_event("ok")]
        final = _make_final_message()
        fake_stream = _FakeStream(events, final)

        with patch("theo.llm.AsyncAnthropic") as mock_cls:
            mock_stream = MagicMock(return_value=fake_stream)
            mock_cls.return_value.messages.stream = mock_stream
            await _collect(
                stream_response(
                    [{"role": "user", "content": "hi"}],
                    system="You are Theo.",
                    speed="reflective",
                )
            )

        call_kwargs = mock_stream.call_args[1]
        assert call_kwargs["system"] == "You are Theo."

    async def test_speed_selects_model(self) -> None:
        events = [_make_text_event("ok")]
        final = _make_final_message()
        fake_stream = _FakeStream(events, final)

        with patch("theo.llm.AsyncAnthropic") as mock_cls:
            mock_stream = MagicMock(return_value=fake_stream)
            mock_cls.return_value.messages.stream = mock_stream
            await _collect(stream_response([{"role": "user", "content": "hi"}], speed="reactive"))

        call_kwargs = mock_stream.call_args[1]
        assert call_kwargs["model"] == "claude-haiku-4-5-20251001"


# ── Retry and error tests ────────────────────────────────────────────


class TestStreamRetries:
    @pytest.fixture(autouse=True)
    def _patch_settings(self) -> Any:
        with patch("theo.llm.get_settings", return_value=_make_settings()):
            yield

    async def test_rate_limit_retries_with_backoff(self) -> None:
        events = [_make_text_event("ok")]
        final = _make_final_message()
        fake_stream = _FakeStream(events, final)

        response = MagicMock()
        response.status_code = 429
        response.headers = {}
        response.json.return_value = {"error": {"message": "rate limited"}}

        error = RateLimitError(
            message="rate limited",
            response=response,
            body={"error": {"message": "rate limited"}},
        )

        call_count = 0

        def side_effect(**_kwargs: Any) -> _FakeStream:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise error
            return fake_stream

        with (
            patch("theo.llm.AsyncAnthropic") as mock_cls,
            patch("theo.llm.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_cls.return_value.messages.stream = MagicMock(side_effect=side_effect)
            result = await _collect(
                stream_response([{"role": "user", "content": "hi"}], speed="reflective")
            )

        assert call_count == 3
        assert isinstance(result[-1], StreamDone)
        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list[0].args[0] == 2
        assert mock_sleep.call_args_list[1].args[0] == 4

    async def test_rate_limit_exhausted_raises(self) -> None:
        response = MagicMock()
        response.status_code = 429
        response.headers = {}
        response.json.return_value = {"error": {"message": "rate limited"}}

        error = RateLimitError(
            message="rate limited",
            response=response,
            body={"error": {"message": "rate limited"}},
        )

        with (
            patch("theo.llm.AsyncAnthropic") as mock_cls,
            patch("theo.llm.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_cls.return_value.messages.stream = MagicMock(side_effect=error)
            with pytest.raises(RateLimitError):
                await _collect(
                    stream_response(
                        [{"role": "user", "content": "hi"}],
                        speed="reflective",
                    )
                )

    async def test_timeout_retries_once(self) -> None:
        events = [_make_text_event("ok")]
        final = _make_final_message()
        fake_stream = _FakeStream(events, final)

        error = APITimeoutError(request=MagicMock())

        call_count = 0

        def side_effect(**_kwargs: Any) -> _FakeStream:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise error
            return fake_stream

        with patch("theo.llm.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.stream = MagicMock(side_effect=side_effect)
            result = await _collect(
                stream_response([{"role": "user", "content": "hi"}], speed="reflective")
            )

        assert call_count == 2
        assert isinstance(result[-1], StreamDone)

    async def test_timeout_exhausted_raises(self) -> None:
        error = APITimeoutError(request=MagicMock())

        with patch("theo.llm.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.stream = MagicMock(side_effect=error)
            with pytest.raises(APITimeoutError):
                await _collect(
                    stream_response(
                        [{"role": "user", "content": "hi"}],
                        speed="reflective",
                    )
                )

    async def test_connection_error_raises_api_unavailable(self) -> None:
        error = APIConnectionError(request=MagicMock(), message="connection refused")

        with patch("theo.llm.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.stream = MagicMock(side_effect=error)
            with pytest.raises(APIUnavailableError, match="connection refused"):
                await _collect(
                    stream_response(
                        [{"role": "user", "content": "hi"}],
                        speed="reflective",
                    )
                )
