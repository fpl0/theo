"""Tests for the Anthropic LLM client (theo.llm)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anthropic import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

from theo.config import Settings
from theo.errors import APIUnavailableError
from theo.llm import (
    SessionContext,
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
    """Base classification tests (no session context)."""

    def test_greeting_is_reactive(self) -> None:
        speed, _ = classify_speed("hello")
        assert speed == "reactive"

    def test_greeting_with_punctuation_is_reactive(self) -> None:
        speed, _ = classify_speed("Hi!")
        assert speed == "reactive"

    def test_thanks_is_reactive(self) -> None:
        speed, _ = classify_speed("thanks")
        assert speed == "reactive"

    def test_ok_is_reactive(self) -> None:
        speed, _ = classify_speed("ok")
        assert speed == "reactive"

    def test_bye_is_reactive(self) -> None:
        speed, _ = classify_speed("bye")
        assert speed == "reactive"

    def test_short_question_is_reflective(self) -> None:
        speed, _ = classify_speed("what's the weather?")
        assert speed == "reflective"

    def test_normal_request_is_reflective(self) -> None:
        speed, _ = classify_speed("tell me about dogs")
        assert speed == "reflective"

    def test_research_keyword_is_deliberative(self) -> None:
        speed, _ = classify_speed("research the best flights to Lisbon and compare options")
        assert speed == "deliberative"

    def test_analyze_keyword_is_deliberative(self) -> None:
        speed, _ = classify_speed("analyze this data set")
        assert speed == "deliberative"

    def test_think_keyword_is_deliberative(self) -> None:
        speed, _ = classify_speed("think about this problem")
        assert speed == "deliberative"

    def test_long_message_is_deliberative(self) -> None:
        speed, _ = classify_speed("a" * 501)
        assert speed == "deliberative"

    def test_whitespace_stripped(self) -> None:
        speed, _ = classify_speed("  hello  ")
        assert speed == "reactive"

    def test_case_insensitive_greeting(self) -> None:
        speed, _ = classify_speed("HELLO")
        assert speed == "reactive"

    def test_case_insensitive_deliberative(self) -> None:
        speed, _ = classify_speed("COMPARE these two")
        assert speed == "deliberative"

    def test_task_indicator_step_by_step(self) -> None:
        speed, signals = classify_speed("Can you give me a step-by-step guide?")
        assert speed == "deliberative"
        assert signals["base_reason"] == "task_indicator"

    def test_task_indicator_pros_and_cons(self) -> None:
        speed, _ = classify_speed("What are the pros and cons of this approach?")
        assert speed == "deliberative"

    def test_returns_signals_dict(self) -> None:
        _, signals = classify_speed("hello")
        assert "base_speed" in signals
        assert "base_reason" in signals
        assert signals["final_reason"] == "no_session_context"


class TestSessionRatchet:
    """Session ratchet and context-aware classification."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self) -> Any:
        with patch("theo.llm.get_settings", return_value=_make_settings()):
            yield

    def test_ratchet_holds_at_peak(self) -> None:
        ctx = SessionContext(peak_speed="deliberative", prior_speeds=("reflective",))
        speed, signals = classify_speed("tell me more", ctx)
        assert speed == "deliberative"
        assert signals.get("ratchet_held") is True

    def test_ratchet_allows_upward_escalation(self) -> None:
        ctx = SessionContext(peak_speed="reflective", prior_speeds=("reflective",))
        speed, _ = classify_speed("analyze this deeply", ctx)
        assert speed == "deliberative"

    def test_ratchet_downgrade_on_reactive_ack(self) -> None:
        ctx = SessionContext(peak_speed="deliberative", prior_speeds=("deliberative",))
        speed, signals = classify_speed("ok", ctx)
        assert speed == "reactive"
        assert signals.get("ratchet_downgrade") is True

    def test_ratchet_holds_reflective_to_deliberative(self) -> None:
        ctx = SessionContext(peak_speed="deliberative", prior_speeds=("deliberative",))
        speed, _ = classify_speed("what about the weather?", ctx)
        assert speed == "deliberative"

    def test_no_ratchet_without_peak(self) -> None:
        ctx = SessionContext(peak_speed=None, prior_speeds=())
        speed, _ = classify_speed("tell me about dogs", ctx)
        assert speed == "reflective"

    def test_ratchet_disabled_in_config(self) -> None:
        with patch(
            "theo.llm.get_settings",
            return_value=_make_settings(session_ratchet_enabled=False),
        ):
            ctx = SessionContext(peak_speed="deliberative", prior_speeds=("reflective",))
            speed, _ = classify_speed("tell me more", ctx)
            assert speed == "reflective"

    def test_history_bias_promotes_reflective(self) -> None:
        ctx = SessionContext(
            peak_speed=None,
            prior_speeds=("deliberative", "deliberative", "reflective"),
        )
        speed, signals = classify_speed("tell me about that topic", ctx)
        assert speed == "deliberative"
        assert signals.get("history_promoted") is True

    def test_history_bias_does_not_promote_reactive(self) -> None:
        ctx = SessionContext(
            peak_speed=None,
            prior_speeds=("deliberative", "deliberative"),
        )
        speed, _ = classify_speed("ok", ctx)
        assert speed == "reactive"

    def test_active_deliberation_forces_deliberative(self) -> None:
        ctx = SessionContext(has_active_deliberation=True)
        speed, signals = classify_speed("what about this?", ctx)
        assert speed == "deliberative"
        assert signals.get("active_deliberation_promoted") is True

    def test_signals_include_effective_speed(self) -> None:
        ctx = SessionContext(peak_speed="deliberative", prior_speeds=("deliberative",))
        speed, signals = classify_speed("tell me more", ctx)
        assert signals["effective_speed"] == speed
        assert signals["base_speed"] == "reflective"


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

    @pytest.fixture(autouse=True)
    def _patch_client(self) -> Any:
        with patch("theo.llm.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.close = AsyncMock()
            self.mock_anthropic = mock_cls
            yield

    async def test_yields_text_deltas(self) -> None:
        events = [_make_text_event("Hello"), _make_text_event(" world")]
        final = _make_final_message()
        fake_stream = _FakeStream(events, final)

        self.mock_anthropic.return_value.messages.stream = MagicMock(return_value=fake_stream)
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

        self.mock_anthropic.return_value.messages.stream = MagicMock(return_value=fake_stream)
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

        self.mock_anthropic.return_value.messages.stream = MagicMock(return_value=fake_stream)
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

        self.mock_anthropic.return_value.messages.stream = MagicMock(return_value=fake_stream)
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

        mock_stream = MagicMock(return_value=fake_stream)
        self.mock_anthropic.return_value.messages.stream = mock_stream
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

        mock_stream = MagicMock(return_value=fake_stream)
        self.mock_anthropic.return_value.messages.stream = mock_stream
        await _collect(stream_response([{"role": "user", "content": "hi"}], speed="reactive"))

        call_kwargs = mock_stream.call_args[1]
        assert call_kwargs["model"] == "claude-haiku-4-5-20251001"


# ── Retry and error tests ────────────────────────────────────────────


class TestStreamRetries:
    @pytest.fixture(autouse=True)
    def _patch_settings(self) -> Any:
        with patch("theo.llm.get_settings", return_value=_make_settings()):
            yield

    @pytest.fixture(autouse=True)
    def _patch_client(self) -> Any:
        with patch("theo.llm.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.close = AsyncMock()
            self.mock_anthropic = mock_cls
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

        with patch("theo.llm.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            self.mock_anthropic.return_value.messages.stream = MagicMock(side_effect=side_effect)
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

        with patch("theo.llm.asyncio.sleep", new_callable=AsyncMock):
            self.mock_anthropic.return_value.messages.stream = MagicMock(side_effect=error)
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

        self.mock_anthropic.return_value.messages.stream = MagicMock(side_effect=side_effect)
        result = await _collect(
            stream_response([{"role": "user", "content": "hi"}], speed="reflective")
        )

        assert call_count == 2
        assert isinstance(result[-1], StreamDone)

    async def test_timeout_exhausted_raises(self) -> None:
        error = APITimeoutError(request=MagicMock())

        self.mock_anthropic.return_value.messages.stream = MagicMock(side_effect=error)
        with pytest.raises(APITimeoutError):
            await _collect(
                stream_response(
                    [{"role": "user", "content": "hi"}],
                    speed="reflective",
                )
            )

    async def test_connection_error_raises_api_unavailable(self) -> None:
        error = APIConnectionError(request=MagicMock(), message="connection refused")

        self.mock_anthropic.return_value.messages.stream = MagicMock(side_effect=error)
        with pytest.raises(APIUnavailableError, match="connection refused"):
            await _collect(
                stream_response(
                    [{"role": "user", "content": "hi"}],
                    speed="reflective",
                )
            )

    async def test_internal_server_error_raises_api_unavailable(self) -> None:
        response = MagicMock()
        response.status_code = 500
        response.headers = {}
        response.json.return_value = {"error": {"message": "internal error"}}

        error = InternalServerError(
            message="internal error",
            response=response,
            body={"error": {"message": "internal error"}},
        )

        self.mock_anthropic.return_value.messages.stream = MagicMock(side_effect=error)
        with pytest.raises(APIUnavailableError, match="internal error"):
            await _collect(
                stream_response(
                    [{"role": "user", "content": "hi"}],
                    speed="reflective",
                )
            )

    async def test_client_closed_on_success(self) -> None:
        events = [_make_text_event("ok")]
        final = _make_final_message()
        fake_stream = _FakeStream(events, final)

        mock_client = self.mock_anthropic.return_value
        mock_client.messages.stream = MagicMock(return_value=fake_stream)
        await _collect(stream_response([{"role": "user", "content": "hi"}], speed="reflective"))

        mock_client.close.assert_awaited_once()

    async def test_client_closed_on_error(self) -> None:
        error = APIConnectionError(request=MagicMock(), message="fail")

        mock_client = self.mock_anthropic.return_value
        mock_client.messages.stream = MagicMock(side_effect=error)
        with pytest.raises(APIUnavailableError):
            await _collect(
                stream_response(
                    [{"role": "user", "content": "hi"}],
                    speed="reflective",
                )
            )

        mock_client.close.assert_awaited_once()
