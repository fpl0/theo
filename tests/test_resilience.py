"""Tests for the resilience module — circuit breaker, retry queue, health check."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from theo import resilience
from theo.bus import EventBus
from theo.bus.events import MessageReceived, ResponseComplete
from theo.conversation import _API_DOWN_ACK, ConversationEngine
from theo.conversation.context import AssembledContext
from theo.errors import APIUnavailableError, CircuitOpenError
from theo.llm import StreamDone, TextDelta
from theo.resilience import CircuitBreaker, HealthStatus, RetryQueue, health_check

# ── Helpers ──────────────────────────────────────────────────────────

_SESSION = uuid4()

_EMPTY_CONTEXT = AssembledContext(
    system_prompt="You are Theo.",
    messages=[],
    token_estimate=10,
)


def _make_msg(
    body: str = "hello",
    *,
    session_id=_SESSION,
    channel="message",
) -> MessageReceived:
    return MessageReceived(body=body, session_id=session_id, channel=channel)


async def _ok_stream(_messages, **_kwargs):
    yield TextDelta(text="ok")
    yield StreamDone(input_tokens=1, output_tokens=1, stop_reason="end_turn")


async def _failing_stream(_messages, **_kwargs):
    raise APIUnavailableError
    yield


# ── Circuit breaker tests ────────────────────────────────────────────


class TestCircuitBreakerTransitions:
    async def test_starts_closed(self) -> None:
        cb = CircuitBreaker()
        assert cb.state == "closed"

    async def test_single_failure_stays_closed(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb._on_failure()
        assert cb.state == "closed"
        assert cb._consecutive_failures == 1

    async def test_threshold_failures_opens_circuit(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb._on_failure()
        assert cb.state == "open"

    async def test_success_resets_failure_count(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb._on_failure()
        cb._on_failure()
        cb._on_success()
        assert cb._consecutive_failures == 0
        assert cb.state == "closed"

    async def test_open_rejects_immediately(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, open_timeout_s=60)
        cb._on_failure()
        assert cb.state == "open"

        async def never_called():
            pytest.fail("should not be called")
            yield  # type: ignore[misc]

        with pytest.raises(CircuitOpenError):
            async for _ in cb.call(never_called()):
                pass

    async def test_half_open_after_timeout(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, open_timeout_s=0.01)
        cb._on_failure()
        assert cb.state == "open"
        await asyncio.sleep(0.02)
        assert cb.state == "half-open"

    async def test_half_open_success_closes(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, open_timeout_s=0.01)
        cb._on_failure()
        await asyncio.sleep(0.02)
        assert cb.state == "half-open"

        async def ok_gen():
            yield TextDelta(text="ok")

        collected = [event async for event in cb.call(ok_gen())]
        assert len(collected) == 1
        assert cb.state == "closed"

    async def test_half_open_failure_reopens(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, open_timeout_s=0.01)
        cb._on_failure()
        await asyncio.sleep(0.02)
        assert cb.state == "half-open"

        async def fail_gen():
            raise APIUnavailableError
            yield

        with pytest.raises(APIUnavailableError):
            async for _ in cb.call(fail_gen()):
                pass
        assert cb.state == "open"

    async def test_half_open_rejects_concurrent_test(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, open_timeout_s=0.01)
        cb._on_failure()
        await asyncio.sleep(0.02)

        # Simulate the lock being held by acquiring it.
        await cb._half_open_lock.acquire()

        async def gen():
            yield TextDelta(text="x")

        with pytest.raises(CircuitOpenError):
            async for _ in cb.call(gen()):
                pass

        cb._half_open_lock.release()

    async def test_closed_passes_events_through(self) -> None:
        cb = CircuitBreaker()

        async def gen():
            yield TextDelta(text="a")
            yield TextDelta(text="b")

        collected = [event async for event in cb.call(gen())]

        assert len(collected) == 2
        assert collected[0] == TextDelta(text="a")
        assert collected[1] == TextDelta(text="b")

    async def test_reset(self) -> None:
        cb = CircuitBreaker(failure_threshold=1)
        cb._on_failure()
        assert cb.state == "open"
        cb.reset()
        assert cb.state == "closed"
        assert cb._consecutive_failures == 0


# ── Retry queue tests ────────────────────────────────────────────────


class TestRetryQueue:
    async def test_enqueue_increases_depth(self) -> None:
        rq = RetryQueue()
        assert rq.depth == 0
        rq.enqueue(session_id=_SESSION, channel="message", body="hi", trust="owner")
        assert rq.depth == 1

    async def test_fifo_order(self) -> None:
        rq = RetryQueue()
        processed: list[str] = []

        async def processor(*, session_id, channel, body, trust):  # noqa: ARG001
            processed.append(body)

        rq.enqueue(session_id=_SESSION, channel="message", body="first", trust="owner")
        rq.enqueue(session_id=_SESSION, channel="message", body="second", trust="owner")
        rq.enqueue(session_id=_SESSION, channel="message", body="third", trust="owner")

        rq.start(processor)
        await asyncio.sleep(0.05)
        await rq.stop()

        assert processed == ["first", "second", "third"]
        assert rq.depth == 0

    async def test_failed_retry_stops_processing(self) -> None:
        rq = RetryQueue()
        processed: list[str] = []

        async def failing_processor(*, session_id, channel, body, trust):  # noqa: ARG001
            if body == "second":
                raise APIUnavailableError
            processed.append(body)

        rq.enqueue(session_id=_SESSION, channel="message", body="first", trust="owner")
        rq.enqueue(session_id=_SESSION, channel="message", body="second", trust="owner")
        rq.enqueue(session_id=_SESSION, channel="message", body="third", trust="owner")

        rq.start(failing_processor)
        await asyncio.sleep(0.05)
        await rq.stop()

        assert processed == ["first"]
        assert rq.depth == 2  # second and third remain

    async def test_wake_triggers_reprocessing(self) -> None:
        rq = RetryQueue()
        processed: list[str] = []
        attempt = 0

        async def recovering_processor(*, session_id, channel, body, trust):  # noqa: ARG001
            nonlocal attempt
            attempt += 1
            if attempt == 1 and body == "msg":
                raise APIUnavailableError
            processed.append(body)

        rq.enqueue(session_id=_SESSION, channel="message", body="msg", trust="owner")

        rq.start(recovering_processor)
        await asyncio.sleep(0.05)
        # First attempt failed; wake to retry.
        rq.wake()
        await asyncio.sleep(0.05)
        await rq.stop()

        assert processed == ["msg"]
        assert rq.depth == 0

    async def test_stop_is_idempotent(self) -> None:
        rq = RetryQueue()
        await rq.stop()
        await rq.stop()


# ── Health check tests ───────────────────────────────────────────────


class TestHealthCheck:
    async def test_healthy_status(self) -> None:
        cb = CircuitBreaker()
        rq = RetryQueue()

        with (
            patch("theo.resilience.health._check_db", AsyncMock(return_value=True)),
            patch("theo.resilience.health._check_telegram", return_value=True),
        ):
            status = await health_check(circuit=cb, queue=rq)

        assert status == HealthStatus(
            db_connected=True,
            api_reachable=True,
            telegram_connected=True,
            circuit_state="closed",
            retry_queue_depth=0,
        )

    async def test_unhealthy_status(self) -> None:
        cb = CircuitBreaker(failure_threshold=1)
        cb._on_failure()
        rq = RetryQueue()
        rq.enqueue(session_id=_SESSION, channel="message", body="hi", trust="owner")

        with (
            patch("theo.resilience.health._check_db", AsyncMock(return_value=False)),
            patch("theo.resilience.health._check_telegram", return_value=False),
        ):
            status = await health_check(circuit=cb, queue=rq)

        assert status.db_connected is False
        assert status.api_reachable is False
        assert status.telegram_connected is False
        assert status.circuit_state == "open"
        assert status.retry_queue_depth == 1

    async def test_api_reachable_reflects_circuit_state(self) -> None:
        cb = CircuitBreaker()
        rq = RetryQueue()

        with (
            patch("theo.resilience.health._check_db", AsyncMock(return_value=True)),
            patch("theo.resilience.health._check_telegram", return_value=True),
        ):
            status = await health_check(circuit=cb, queue=rq)
            assert status.api_reachable is True

            for _ in range(3):
                cb._on_failure()
            status = await health_check(circuit=cb, queue=rq)
            assert status.api_reachable is False


# ── Integration: conversation engine + circuit breaker ───────────────


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
    """Auto-incrementing episode store mock."""
    episode_id = 0

    async def _store(**_kwargs):
        nonlocal episode_id
        episode_id += 1
        return episode_id

    with patch("theo.conversation.turn.store_episode", AsyncMock(side_effect=_store)) as mock:
        yield mock


@pytest.fixture
def mock_assemble():
    """Return an empty context for every assemble call."""
    with patch("theo.conversation.turn.assemble", AsyncMock(return_value=_EMPTY_CONTEXT)) as mock:
        yield mock


@pytest.fixture
def fresh_circuit():
    """Reset the module-level circuit breaker for each test."""
    resilience.circuit_breaker.reset()
    yield resilience.circuit_breaker
    resilience.circuit_breaker.reset()


@pytest.fixture
def fresh_retry_queue():
    """Provide a fresh retry queue for each test."""
    original = resilience.retry_queue
    fresh = RetryQueue()
    resilience.retry_queue = fresh
    with (
        patch("theo.conversation.turn.retry_queue", fresh),
        patch("theo.conversation.engine.retry_queue", fresh),
    ):
        yield fresh
    resilience.retry_queue = original


class TestConversationIntegration:
    async def test_api_failure_sends_ack_and_enqueues(
        self,
        mock_bus,
        mock_store_episode,
        mock_assemble,
        fresh_circuit,
        fresh_retry_queue,
    ) -> None:
        _ = mock_store_episode, mock_assemble, fresh_circuit
        _bus, published = mock_bus

        with patch("theo.conversation.turn.stream_response", _failing_stream):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg(body="test message"))

        ack_events = [e for e in published if isinstance(e, ResponseComplete)]
        assert len(ack_events) == 1
        assert ack_events[0].body == _API_DOWN_ACK
        assert fresh_retry_queue.depth == 1

    async def test_circuit_opens_after_threshold_failures(
        self,
        mock_bus,
        mock_store_episode,
        mock_assemble,
        fresh_circuit,
        fresh_retry_queue,
    ) -> None:
        _ = mock_bus, mock_store_episode, mock_assemble, fresh_retry_queue

        with patch("theo.conversation.turn.stream_response", _failing_stream):
            eng = ConversationEngine()
            eng._state = "running"
            for _ in range(3):
                await eng._process_message(_make_msg())

        assert fresh_circuit.state == "open"
        assert fresh_retry_queue.depth == 3

    async def test_open_circuit_rejects_immediately(
        self,
        mock_bus,
        mock_store_episode,
        mock_assemble,
        fresh_circuit,
        fresh_retry_queue,
    ) -> None:
        _ = mock_store_episode, mock_assemble, fresh_retry_queue
        _bus, published = mock_bus

        # Open the circuit directly.
        for _ in range(3):
            fresh_circuit._on_failure()
        assert fresh_circuit.state == "open"

        with patch("theo.conversation.turn.stream_response", _ok_stream):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg())

        # Circuit is open: user gets ack, no real response.
        ack_events = [e for e in published if isinstance(e, ResponseComplete)]
        assert len(ack_events) == 1
        assert ack_events[0].body == _API_DOWN_ACK

    async def test_successful_turn_with_circuit_breaker(
        self,
        mock_bus,
        mock_store_episode,
        mock_assemble,
        fresh_circuit,
        fresh_retry_queue,
    ) -> None:
        _ = mock_store_episode, mock_assemble, fresh_retry_queue
        _bus, published = mock_bus

        with patch("theo.conversation.turn.stream_response", _ok_stream):
            eng = ConversationEngine()
            eng._state = "running"
            await eng._process_message(_make_msg())

        complete = [e for e in published if isinstance(e, ResponseComplete)]
        assert len(complete) == 1
        assert complete[0].body == "ok"
        assert fresh_circuit.state == "closed"

    async def test_successful_turn_wakes_retry_queue(
        self,
        mock_bus,
        mock_store_episode,
        mock_assemble,
        fresh_circuit,
        fresh_retry_queue,
    ) -> None:
        _ = mock_bus, mock_store_episode, mock_assemble, fresh_circuit
        fresh_retry_queue.enqueue(
            session_id=_SESSION, channel="message", body="queued", trust="owner"
        )

        with patch("theo.conversation.turn.stream_response", _ok_stream):
            eng = ConversationEngine()
            eng._state = "running"

            retried: list[str] = []

            async def capture_retry(*, session_id, channel, body, trust):  # noqa: ARG001
                retried.append(body)

            fresh_retry_queue.start(capture_retry)
            await eng._process_message(_make_msg())
            await asyncio.sleep(0.05)
            await fresh_retry_queue.stop()

        assert retried == ["queued"]
