"""Tests for the conversation engine."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from theo.bus import EventBus
from theo.bus.events import MessageReceived, ResponseChunk, ResponseComplete
from theo.context import AssembledContext
from theo.conversation import ConversationEngine
from theo.errors import ConversationNotRunningError
from theo.llm import StreamDone, TextDelta

# ── Helpers ──────────────────────────────────────────────────────────

_SESSION = uuid4()

_EMPTY_CONTEXT = AssembledContext(
    system_prompt="You are Theo.",
    messages=[],
    token_estimate=10,
)


def _make_msg(body: str = "hello", *, session_id=_SESSION, channel="message") -> MessageReceived:
    return MessageReceived(body=body, session_id=session_id, channel=channel)


async def _fake_stream(_messages, *, system=None, speed="reflective", tools=None, max_tokens=None):  # noqa: ARG001
    """Simulate a streaming response yielding two text chunks and done."""
    yield TextDelta(text="Hello")
    yield TextDelta(text=" world")
    yield StreamDone(input_tokens=10, output_tokens=5, stop_reason="end_turn")


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_bus():
    """Provide a mock EventBus that tracks published events."""
    event_bus = EventBus.__new__(EventBus)
    event_bus._handlers = {}  # noqa: SLF001
    event_bus._queue = asyncio.Queue()  # noqa: SLF001
    event_bus._task = None  # noqa: SLF001
    event_bus._running = True  # noqa: SLF001
    published: list = []

    async def fake_publish(event):
        published.append(event)

    event_bus.publish = AsyncMock(side_effect=fake_publish)
    event_bus.subscribe = lambda _et, _h: None

    with patch("theo.conversation.bus", event_bus):
        yield event_bus, published


@pytest.fixture
def mock_store_episode():
    episode_id = 0

    async def _store(**_kwargs):
        nonlocal episode_id
        episode_id += 1
        return episode_id

    with patch("theo.conversation.store_episode", AsyncMock(side_effect=_store)) as mock:
        yield mock


@pytest.fixture
def mock_assemble():
    with patch("theo.conversation.assemble", AsyncMock(return_value=_EMPTY_CONTEXT)) as mock:
        yield mock


@pytest.fixture
def mock_stream():
    with patch("theo.conversation.stream_response", _fake_stream):
        yield


@pytest.fixture
def engine(_mock_bus, _mock_store_episode, _mock_assemble, _mock_stream):
    """Fully mocked ConversationEngine."""
    eng = ConversationEngine()
    eng._state = "running"  # noqa: SLF001
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
        await engine._process_message(_make_msg())  # noqa: SLF001
        complete_events = [e for e in published if isinstance(e, ResponseComplete)]
        assert len(complete_events) == 1
        assert complete_events[0].body == "Hello world"
        assert complete_events[0].session_id == _SESSION

    async def test_response_chunks_published_during_streaming(self, engine, mock_bus) -> None:
        _bus, published = mock_bus
        await engine._process_message(_make_msg())  # noqa: SLF001
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

        with patch("theo.conversation.stream_response", tracking_stream):
            eng = ConversationEngine()
            eng._state = "running"  # noqa: SLF001
            await eng._process_message(_make_msg())  # noqa: SLF001

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

        with patch("theo.conversation.stream_response", tracking_stream):
            eng = ConversationEngine()
            eng._state = "running"  # noqa: SLF001
            await eng._process_message(_make_msg())  # noqa: SLF001

        assert call_order == ["store_user", "stream", "store_assistant"]

    async def test_episode_id_set_on_response_complete(self, engine, mock_bus) -> None:
        _bus, published = mock_bus
        await engine._process_message(_make_msg())  # noqa: SLF001
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
        await engine._process_message(msg)  # noqa: SLF001
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

        with patch("theo.conversation.stream_response", capture_stream):
            eng = ConversationEngine()
            eng._state = "running"  # noqa: SLF001
            await eng._process_message(_make_msg())  # noqa: SLF001

        assert streamed_kwargs[0]["system"] == "You are Theo."

    async def test_empty_system_prompt_passed_as_none(self, mock_bus, mock_store_episode) -> None:
        _ = mock_bus, mock_store_episode  # activate fixtures
        empty_ctx = AssembledContext(system_prompt="", messages=[], token_estimate=0)
        streamed_kwargs: list[dict] = []

        async def capture_stream(_messages, **kwargs):
            streamed_kwargs.append(kwargs)
            yield TextDelta(text="ok")
            yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="end_turn")

        with (
            patch("theo.conversation.assemble", AsyncMock(return_value=empty_ctx)),
            patch("theo.conversation.stream_response", capture_stream),
        ):
            eng = ConversationEngine()
            eng._state = "running"  # noqa: SLF001
            await eng._process_message(_make_msg())  # noqa: SLF001

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

        with patch("theo.conversation.stream_response", capture_stream):
            eng = ConversationEngine()
            eng._state = "running"  # noqa: SLF001
            await eng._process_message(_make_msg(body="hi"))  # noqa: SLF001

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

        with patch("theo.conversation.stream_response", capture_stream):
            eng = ConversationEngine()
            eng._state = "running"  # noqa: SLF001
            await eng._process_message(  # noqa: SLF001
                _make_msg(body="please analyze this data carefully")
            )

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
        eng._state = "stopped"  # noqa: SLF001
        with pytest.raises(ConversationNotRunningError):
            await eng._on_message(_make_msg())  # noqa: SLF001

    async def test_paused_engine_queues_messages(self, engine, mock_bus) -> None:
        _ = mock_bus
        engine._state = "paused"  # noqa: SLF001
        await engine._on_message(_make_msg())  # noqa: SLF001
        assert engine._paused_queue.qsize() == 1  # noqa: SLF001

    async def test_resume_drains_paused_queue(self, engine, mock_bus) -> None:
        _bus, published = mock_bus

        engine._state = "paused"  # noqa: SLF001
        await engine._on_message(_make_msg(body="queued"))  # noqa: SLF001
        assert engine._paused_queue.qsize() == 1  # noqa: SLF001

        engine._state = "paused"  # noqa: SLF001
        await engine.resume()

        complete_events = [e for e in published if isinstance(e, ResponseComplete)]
        assert len(complete_events) == 1
        assert engine._paused_queue.empty()  # noqa: SLF001

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

        with patch("theo.conversation.stream_response", slow_stream):
            eng = ConversationEngine()
            eng._state = "running"  # noqa: SLF001

            msg1 = _make_msg(body="first")
            msg2 = _make_msg(body="second")

            # Launch both concurrently — the lock should serialize them.
            await asyncio.gather(
                eng._process_message(msg1),  # noqa: SLF001
                eng._process_message(msg2),  # noqa: SLF001
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

        with patch("theo.conversation.stream_response", concurrent_stream):
            eng = ConversationEngine()
            eng._state = "running"  # noqa: SLF001

            msg1 = _make_msg(body="a", session_id=uuid4())
            msg2 = _make_msg(body="b", session_id=uuid4())

            await asyncio.gather(
                eng._process_message(msg1),  # noqa: SLF001
                eng._process_message(msg2),  # noqa: SLF001
            )

        assert max_concurrent == 2

    async def test_message_without_session_id_is_dropped(self, engine, mock_bus) -> None:
        _bus, published = mock_bus
        msg = MessageReceived(body="no session", session_id=None)
        await engine._process_message(msg)  # noqa: SLF001
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

        with patch("theo.conversation.stream_response", slow_stream):
            eng = ConversationEngine()
            eng._state = "running"  # noqa: SLF001

            # Start processing in background.
            task = asyncio.create_task(eng._process_message(_make_msg()))  # noqa: SLF001
            await asyncio.sleep(0.01)  # let it acquire the lock

            # Stop should wait for the in-flight turn.
            await eng.stop()
            await task

        assert completed is True
