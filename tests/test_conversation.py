"""Tests for the conversation engine."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from theo.bus import EventBus
from theo.bus.events import MessageReceived, ResponseChunk, ResponseComplete
from theo.conversation import _MAX_TOOL_ITERATIONS, ConversationEngine
from theo.conversation.context import AssembledContext, SectionTokens
from theo.errors import CircuitOpenError, ConversationNotRunningError
from theo.llm import StreamDone, TextDelta, ToolUseRequest
from theo.resilience import CircuitBreaker, RetryQueue

# ── Helpers ──────────────────────────────────────────────────────────

_SESSION = uuid4()

_ZERO_TOKENS = SectionTokens(persona=0, goals=0, user_model=0, current_task=0, memory=0, history=0)

_EMPTY_CONTEXT = AssembledContext(
    system_prompt="You are Theo.",
    messages=[],
    token_estimate=10,
    section_tokens=_ZERO_TOKENS,
)


def _make_msg(body: str = "hello", *, session_id=_SESSION, channel="message") -> MessageReceived:
    return MessageReceived(body=body, session_id=session_id, channel=channel)


async def _fake_stream(_messages, *, system=None, speed="reflective", tools=None, max_tokens=None):  # noqa: ARG001
    """Simulate a streaming response yielding two text chunks and done."""
    yield TextDelta(text="Hello")
    yield TextDelta(text=" world")
    yield StreamDone(input_tokens=10, output_tokens=5, stop_reason="end_turn")


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_resilience():
    """Provide fresh circuit breaker and retry queue per test."""
    fresh_cb = CircuitBreaker()
    fresh_rq = RetryQueue()
    with (
        patch("theo.conversation.turn.circuit_breaker", fresh_cb),
        patch("theo.conversation.turn.retry_queue", fresh_rq),
        patch("theo.conversation.engine.retry_queue", fresh_rq),
    ):
        yield


@pytest.fixture
def mock_bus():
    """Provide a mock EventBus that tracks published events."""
    event_bus = EventBus.__new__(EventBus)
    event_bus._handlers = {}
    event_bus._queue = asyncio.Queue()
    event_bus._task = None
    event_bus._running = True
    published: list = []

    async def fake_publish(event):
        published.append(event)

    event_bus.publish = AsyncMock(side_effect=fake_publish)
    event_bus.subscribe = lambda _et, _h: None

    with (
        patch("theo.conversation.turn.bus", event_bus),
        patch("theo.conversation.engine.bus", event_bus),
    ):
        yield event_bus, published


@pytest.fixture
def mock_store_episode():
    episode_id = 0

    async def _store(**_kwargs):
        nonlocal episode_id
        episode_id += 1
        return episode_id

    with patch("theo.conversation.turn.store_episode", AsyncMock(side_effect=_store)) as mock:
        yield mock


@pytest.fixture
def mock_assemble():
    with patch("theo.conversation.turn.assemble", AsyncMock(return_value=_EMPTY_CONTEXT)) as mock:
        yield mock


@pytest.fixture
def mock_stream():
    with patch("theo.conversation.turn.stream_response", _fake_stream):
        yield


@pytest.fixture
def engine(_mock_bus, _mock_store_episode, _mock_assemble, _mock_stream):
    """Fully mocked ConversationEngine."""
    eng = ConversationEngine()
    eng._state = "running"
    return eng


# Alias fixtures so they can be requested by the underscore-prefixed names
# that the `engine` fixture uses, while keeping the original names usable.
_mock_bus = pytest.fixture(name="_mock_bus")(lambda mock_bus: mock_bus)
_mock_store_episode = pytest.fixture(name="_mock_store_episode")(
    lambda mock_store_episode: mock_store_episode
)
_mock_assemble = pytest.fixture(name="_mock_assemble")(lambda mock_assemble: mock_assemble)
_mock_stream = pytest.fixture(name="_mock_stream")(lambda mock_stream: mock_stream)


# ── Full loop tests ──────────────────────────────────────────────────


class TestFullLoop:
    async def test_message_produces_response_complete(self, engine, mock_bus) -> None:
        _bus, published = mock_bus
        await engine._process_message(_make_msg())
        complete_events = [e for e in published if isinstance(e, ResponseComplete)]
        assert len(complete_events) == 1
        assert complete_events[0].body == "Hello world"
        assert complete_events[0].session_id == _SESSION

    async def test_response_chunks_published_during_streaming(self, engine, mock_bus) -> None:
        _bus, published = mock_bus
        await engine._process_message(_make_msg())
        chunks = [e for e in published if isinstance(e, ResponseChunk)]
        assert len(chunks) == 2
        assert chunks[0].body == "Hello"
        assert chunks[1].body == " world"

    async def test_incoming_message_persisted_before_llm_call(
        self, mock_bus, mock_assemble, mock_store_episode
    ) -> None:
        """The user message must be persisted as an episode before calling Claude."""
        _ = mock_bus, mock_assemble  # activate fixtures
        call_order: list[str] = []

        original_store = mock_store_episode.side_effect

        async def tracking_store(**kwargs):
            call_order.append(f"store_{kwargs['role']}")
            return await original_store(**kwargs)

        mock_store_episode.side_effect = tracking_store

        async def tracking_stream(_messages, **_kwargs):
            call_order.append("stream")
            yield TextDelta(text="ok")
            yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="end_turn")

        with patch("theo.conversation.turn.stream_response", tracking_stream):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg())

        assert call_order.index("store_user") < call_order.index("stream")

    async def test_assistant_response_persisted_after_streaming(
        self, mock_bus, mock_assemble, mock_store_episode
    ) -> None:
        _ = mock_bus, mock_assemble  # activate fixtures
        call_order: list[str] = []

        original_store = mock_store_episode.side_effect

        async def tracking_store(**kwargs):
            call_order.append(f"store_{kwargs['role']}")
            return await original_store(**kwargs)

        mock_store_episode.side_effect = tracking_store

        async def tracking_stream(_messages, **_kwargs):
            call_order.append("stream")
            yield TextDelta(text="reply")
            yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="end_turn")

        with patch("theo.conversation.turn.stream_response", tracking_stream):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg())

        assert call_order == ["store_user", "stream", "store_assistant"]

    async def test_episode_id_set_on_response_complete(self, engine, mock_bus) -> None:
        _bus, published = mock_bus
        await engine._process_message(_make_msg())
        complete = next(e for e in published if isinstance(e, ResponseComplete))
        # store_episode returns incrementing ints: 1 for user, 2 for assistant
        assert complete.episode_id == 2


# ── Context assembly tests ───────────────────────────────────────────


class TestContextAssembly:
    async def test_assemble_called_with_session_and_message(
        self, engine, mock_bus, mock_assemble
    ) -> None:
        _ = mock_bus  # activate fixture
        msg = _make_msg(body="what is the weather?")
        await engine._process_message(msg)
        mock_assemble.assert_awaited_once_with(
            session_id=_SESSION,
            latest_message="what is the weather?",
        )

    async def test_system_prompt_passed_to_stream(
        self, mock_bus, mock_store_episode, mock_assemble
    ) -> None:
        _ = mock_bus, mock_store_episode, mock_assemble  # activate fixtures
        streamed_kwargs: list[dict] = []

        async def capture_stream(_messages, **kwargs):
            streamed_kwargs.append(kwargs)
            yield TextDelta(text="ok")
            yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="end_turn")

        with patch("theo.conversation.turn.stream_response", capture_stream):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg())

        assert streamed_kwargs[0]["system"] == "You are Theo."

    async def test_empty_system_prompt_passed_as_none(self, mock_bus, mock_store_episode) -> None:
        _ = mock_bus, mock_store_episode  # activate fixtures
        empty_ctx = AssembledContext(
            system_prompt="",
            messages=[],
            token_estimate=0,
            section_tokens=_ZERO_TOKENS,
        )
        streamed_kwargs: list[dict] = []

        async def capture_stream(_messages, **kwargs):
            streamed_kwargs.append(kwargs)
            yield TextDelta(text="ok")
            yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="end_turn")

        with (
            patch("theo.conversation.turn.assemble", AsyncMock(return_value=empty_ctx)),
            patch("theo.conversation.turn.stream_response", capture_stream),
        ):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg())

        assert streamed_kwargs[0]["system"] is None


# ── Speed classification tests ───────────────────────────────────────


class TestSpeedSelection:
    async def test_reactive_message_uses_reactive_speed(
        self, mock_bus, mock_store_episode, mock_assemble
    ) -> None:
        _ = mock_bus, mock_store_episode, mock_assemble  # activate fixtures
        streamed_kwargs: list[dict] = []

        async def capture_stream(_messages, **kwargs):
            streamed_kwargs.append(kwargs)
            yield TextDelta(text="hi")
            yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="end_turn")

        with patch("theo.conversation.turn.stream_response", capture_stream):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg(body="hi"))

        assert streamed_kwargs[0]["speed"] == "reactive"

    async def test_deliberative_message_uses_deliberative_speed(
        self, mock_bus, mock_store_episode, mock_assemble
    ) -> None:
        _ = mock_bus, mock_store_episode, mock_assemble  # activate fixtures
        streamed_kwargs: list[dict] = []

        async def capture_stream(_messages, **kwargs):
            streamed_kwargs.append(kwargs)
            yield TextDelta(text="ok")
            yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="end_turn")

        with patch("theo.conversation.turn.stream_response", capture_stream):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg(body="please analyze this data carefully"))

        assert streamed_kwargs[0]["speed"] == "deliberative"


# ── State management tests ───────────────────────────────────────────


class TestStateManagement:
    async def test_initial_state_is_stopped(self) -> None:
        eng = ConversationEngine()
        assert eng.state == "stopped"

    async def test_start_sets_running(self, mock_bus) -> None:
        _ = mock_bus
        eng = ConversationEngine()
        await eng.start()
        assert eng.state == "running"

    async def test_pause_sets_paused(self, mock_bus) -> None:
        _ = mock_bus
        eng = ConversationEngine()
        await eng.start()
        eng.pause()
        assert eng.state == "paused"

    async def test_resume_sets_running(self, mock_bus) -> None:
        _ = mock_bus
        eng = ConversationEngine()
        await eng.start()
        eng.pause()
        await eng.resume()
        assert eng.state == "running"

    async def test_stop_sets_stopped(self, mock_bus) -> None:
        _ = mock_bus
        eng = ConversationEngine()
        await eng.start()
        await eng.stop()
        assert eng.state == "stopped"

    async def test_stopped_engine_raises_on_message(self, mock_bus) -> None:
        _ = mock_bus
        eng = ConversationEngine()
        eng._state = "stopped"
        with pytest.raises(ConversationNotRunningError):
            await eng._on_message(_make_msg())

    async def test_paused_engine_queues_messages(self, engine, mock_bus) -> None:
        _ = mock_bus
        engine._state = "paused"
        await engine._on_message(_make_msg())
        assert engine._paused_queue.qsize() == 1

    async def test_resume_drains_paused_queue(self, engine, mock_bus) -> None:
        _bus, published = mock_bus

        engine._state = "paused"
        await engine._on_message(_make_msg(body="queued"))
        assert engine._paused_queue.qsize() == 1

        engine._state = "paused"
        await engine.resume()

        complete_events = [e for e in published if isinstance(e, ResponseComplete)]
        assert len(complete_events) == 1
        assert engine._paused_queue.empty()

    async def test_stop_is_idempotent(self, mock_bus) -> None:
        _ = mock_bus
        eng = ConversationEngine()
        await eng.stop()
        await eng.stop()  # should not raise

    async def test_start_is_idempotent(self, mock_bus) -> None:
        _ = mock_bus
        eng = ConversationEngine()
        await eng.start()
        await eng.start()  # should not raise
        assert eng.state == "running"

    async def test_pause_noop_when_not_running(self, mock_bus) -> None:
        _ = mock_bus
        eng = ConversationEngine()
        eng.pause()  # stopped → pause is a no-op
        assert eng.state == "stopped"

    async def test_resume_noop_when_not_paused(self, mock_bus) -> None:
        _ = mock_bus
        eng = ConversationEngine()
        await eng.start()
        await eng.resume()  # running → resume is a no-op
        assert eng.state == "running"

    async def test_kill_sets_stopped(self, mock_bus) -> None:
        _ = mock_bus
        eng = ConversationEngine()
        await eng.start()
        eng.kill()
        assert eng.state == "stopped"

    async def test_kill_sets_drained_event(self) -> None:
        eng = ConversationEngine()
        eng._drained.clear()
        eng.kill()
        assert eng._drained.is_set()

    async def test_kill_from_paused(self, mock_bus) -> None:
        _ = mock_bus
        eng = ConversationEngine()
        await eng.start()
        eng.pause()
        eng.kill()
        assert eng.state == "stopped"

    def test_inflight_property(self) -> None:
        eng = ConversationEngine()
        assert eng.inflight == 0
        eng._inflight = 3
        assert eng.inflight == 3

    def test_queue_depth_property(self) -> None:
        eng = ConversationEngine()
        assert eng.queue_depth == 0
        dummy = MessageReceived(
            session_id=uuid4(), channel="message", body="test", role="user", trust="owner"
        )
        eng._paused_queue.put_nowait(dummy)
        assert eng.queue_depth == 1


# ── Sequential processing tests ──────────────────────────────────────


class TestConcurrency:
    async def test_same_session_messages_processed_sequentially(
        self, mock_bus, mock_assemble, mock_store_episode
    ) -> None:
        """Two messages on the same session must not interleave."""
        _ = mock_bus, mock_assemble, mock_store_episode  # activate fixtures
        processing_order: list[str] = []

        async def slow_stream(messages, **_kwargs):
            body = messages[-1]["content"]
            processing_order.append(f"start_{body}")
            await asyncio.sleep(0.05)
            processing_order.append(f"end_{body}")
            yield TextDelta(text="reply")
            yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="end_turn")

        with patch("theo.conversation.turn.stream_response", slow_stream):
            eng = ConversationEngine()
            eng._state = "running"

            msg1 = _make_msg(body="first")
            msg2 = _make_msg(body="second")

            # Launch both concurrently — the lock should serialize them.
            await asyncio.gather(
                eng._process_message(msg1),
                eng._process_message(msg2),
            )

        # Must complete one fully before starting the next.
        first_end = processing_order.index("end_first")
        second_start = processing_order.index("start_second")
        second_end = processing_order.index("end_second")
        first_start = processing_order.index("start_first")
        assert first_end < second_start or second_end < first_start

    async def test_different_sessions_can_run_concurrently(
        self, mock_bus, mock_assemble, mock_store_episode
    ) -> None:
        """Messages from different sessions may overlap."""
        _ = mock_bus, mock_assemble, mock_store_episode  # activate fixtures
        active: list[int] = []
        max_concurrent = 0

        async def concurrent_stream(_messages, **_kwargs):
            nonlocal max_concurrent
            active.append(1)
            max_concurrent = max(max_concurrent, len(active))
            await asyncio.sleep(0.05)
            active.pop()
            yield TextDelta(text="reply")
            yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="end_turn")

        with patch("theo.conversation.turn.stream_response", concurrent_stream):
            eng = ConversationEngine()
            eng._state = "running"

            msg1 = _make_msg(body="a", session_id=uuid4())
            msg2 = _make_msg(body="b", session_id=uuid4())

            await asyncio.gather(
                eng._process_message(msg1),
                eng._process_message(msg2),
            )

        assert max_concurrent == 2

    async def test_message_without_session_id_is_dropped(self, engine, mock_bus) -> None:
        _bus, published = mock_bus
        msg = MessageReceived(body="no session", session_id=None)
        await engine._process_message(msg)
        assert len(published) == 0


# ── Drain / shutdown tests ───────────────────────────────────────────


class TestDrain:
    async def test_stop_waits_for_inflight_turns(
        self, mock_bus, mock_assemble, mock_store_episode
    ) -> None:
        _ = mock_bus, mock_assemble, mock_store_episode  # activate fixtures
        completed = False

        async def slow_stream(_messages, **_kwargs):
            nonlocal completed
            await asyncio.sleep(0.1)
            completed = True
            yield TextDelta(text="done")
            yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="end_turn")

        with patch("theo.conversation.turn.stream_response", slow_stream):
            eng = ConversationEngine()
            eng._state = "running"

            # Start processing in background.
            task = asyncio.create_task(eng._process_message(_make_msg()))
            await asyncio.sleep(0.01)  # let it acquire the lock

            # Stop should wait for the in-flight turn.
            await eng.stop()
            await task

        assert completed is True


# ── Tool loop tests ──────────────────────────────────────────────────


class TestToolLoop:
    async def test_single_tool_call_produces_final_response(
        self, mock_bus, mock_assemble, mock_store_episode
    ) -> None:
        """Claude calls one tool, gets result, then produces text."""
        _ = mock_bus, mock_assemble, mock_store_episode
        _bus, published = mock_bus
        call_count = 0

        async def tool_then_text(_messages, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield ToolUseRequest(id="t1", name="search_memory", input={"query": "test"})
                yield StreamDone(input_tokens=10, output_tokens=5, stop_reason="tool_use")
            else:
                yield TextDelta(text="Found it!")
                yield StreamDone(input_tokens=15, output_tokens=8, stop_reason="end_turn")

        tool_result = '[{"body":"result"}]'
        with (
            patch("theo.conversation.turn.stream_response", tool_then_text),
            patch("theo.conversation.turn.execute_tool", AsyncMock(return_value=tool_result)),
        ):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg())

        complete = [e for e in published if isinstance(e, ResponseComplete)]
        assert len(complete) == 1
        assert complete[0].body == "Found it!"
        assert call_count == 2

    async def test_multi_tool_turn(self, mock_bus, mock_assemble, mock_store_episode) -> None:
        """Claude calls multiple tools in a single response."""
        _ = mock_bus, mock_assemble, mock_store_episode
        _bus, published = mock_bus
        tool_calls: list[str] = []

        async def two_tools_then_text(messages, **_kwargs):
            has_tool_results = any(
                isinstance(m.get("content"), list)
                and any(
                    isinstance(item, dict) and item.get("type") == "tool_result"
                    for item in m["content"]
                )
                for m in messages
                if isinstance(m, dict)
            )
            if not has_tool_results:
                yield ToolUseRequest(
                    id="t1",
                    name="store_memory",
                    input={"kind": "fact", "body": "a"},
                )
                yield ToolUseRequest(id="t2", name="search_memory", input={"query": "b"})
                yield StreamDone(input_tokens=10, output_tokens=5, stop_reason="tool_use")
            else:
                yield TextDelta(text="Done")
                yield StreamDone(input_tokens=20, output_tokens=3, stop_reason="end_turn")

        async def tracking_execute(name, _tool_input, **_kwargs):
            tool_calls.append(name)
            return '{"ok": true}'

        with (
            patch("theo.conversation.turn.stream_response", two_tools_then_text),
            patch("theo.conversation.turn.execute_tool", AsyncMock(side_effect=tracking_execute)),
        ):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg())

        assert tool_calls == ["store_memory", "search_memory"]
        complete = [e for e in published if isinstance(e, ResponseComplete)]
        assert complete[0].body == "Done"

    async def test_tool_error_returned_to_claude(
        self, mock_bus, mock_assemble, mock_store_episode
    ) -> None:
        """Tool errors are sent back to Claude as results, not raised."""
        _ = mock_bus, mock_assemble, mock_store_episode
        _bus, published = mock_bus
        call_count = 0

        async def tool_then_text(_messages, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield ToolUseRequest(
                    id="t1",
                    name="store_memory",
                    input={"kind": "x", "body": "y"},
                )
                yield StreamDone(input_tokens=10, output_tokens=5, stop_reason="tool_use")
            else:
                yield TextDelta(text="I see there was an error")
                yield StreamDone(input_tokens=15, output_tokens=8, stop_reason="end_turn")

        with (
            patch("theo.conversation.turn.stream_response", tool_then_text),
            patch(
                "theo.conversation.turn.execute_tool",
                AsyncMock(return_value="Error executing store_memory: db down"),
            ),
        ):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg())

        complete = [e for e in published if isinstance(e, ResponseComplete)]
        assert len(complete) == 1
        assert complete[0].body == "I see there was an error"

    async def test_max_iterations_enforced(
        self, mock_bus, mock_assemble, mock_store_episode
    ) -> None:
        """Tool loop stops after _MAX_TOOL_ITERATIONS even if tools keep coming."""
        _ = mock_bus, mock_assemble, mock_store_episode
        _bus, published = mock_bus
        call_count = 0

        async def always_tool(_messages, **_kwargs):
            nonlocal call_count
            call_count += 1
            yield ToolUseRequest(id=f"t{call_count}", name="search_memory", input={"query": "x"})
            yield StreamDone(input_tokens=10, output_tokens=5, stop_reason="tool_use")

        with (
            patch("theo.conversation.turn.stream_response", always_tool),
            patch("theo.conversation.turn.execute_tool", AsyncMock(return_value='{"ok": true}')),
        ):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg())

        assert call_count == _MAX_TOOL_ITERATIONS
        complete = [e for e in published if isinstance(e, ResponseComplete)]
        assert len(complete) == 1

    async def test_text_and_tool_in_same_response(
        self, mock_bus, mock_assemble, mock_store_episode
    ) -> None:
        """Claude can emit text and tool calls in the same stream."""
        _ = mock_bus, mock_assemble, mock_store_episode
        _bus, published = mock_bus
        call_count = 0

        async def text_and_tool(_messages, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield TextDelta(text="Let me check... ")
                yield ToolUseRequest(id="t1", name="search_memory", input={"query": "weather"})
                yield StreamDone(input_tokens=10, output_tokens=5, stop_reason="tool_use")
            else:
                yield TextDelta(text="Here's what I found.")
                yield StreamDone(input_tokens=15, output_tokens=8, stop_reason="end_turn")

        with (
            patch("theo.conversation.turn.stream_response", text_and_tool),
            patch(
                "theo.conversation.turn.execute_tool", AsyncMock(return_value='{"results": []}')
            ),
        ):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg())

        complete = [e for e in published if isinstance(e, ResponseComplete)]
        assert complete[0].body == "Let me check... Here's what I found."
        chunks = [e for e in published if isinstance(e, ResponseChunk)]
        assert len(chunks) == 2


# ── API failure handling tests ────────────────────────────────────────


class TestAPIFailureHandling:
    async def test_circuit_open_error_sends_ack_and_enqueues(
        self, mock_bus, mock_assemble, mock_store_episode
    ) -> None:
        """CircuitOpenError must be caught and trigger retry queue."""
        _ = mock_assemble, mock_store_episode
        _bus, published = mock_bus

        async def _open_breaker(_stream):
            if False:
                yield  # pragma: no cover — yield required to make async generator
            raise CircuitOpenError

        fresh_cb = CircuitBreaker()
        fresh_cb.call = _open_breaker  # type: ignore[assignment]
        fresh_rq = RetryQueue()

        with (
            patch("theo.conversation.turn.stream_response", _fake_stream),
            patch("theo.conversation.turn.circuit_breaker", fresh_cb),
            patch("theo.conversation.turn.retry_queue", fresh_rq),
            patch("theo.conversation.engine.retry_queue", fresh_rq),
        ):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg())

        complete = [e for e in published if isinstance(e, ResponseComplete)]
        assert len(complete) == 1
        assert "trouble" in complete[0].body.lower()
        assert fresh_rq.depth == 1
