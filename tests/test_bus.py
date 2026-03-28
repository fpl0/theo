"""Tests for the event bus and event models."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from theo.bus import EventBus
from theo.bus.events import (
    MessageReceived,
    ResponseChunk,
    ResponseComplete,
    SystemEvent,
)
from theo.errors import BusNotRunningError

# ── Event model tests ────────────────────────────────────────────────


class TestEventModels:
    def test_base_event_assigns_id_and_timestamp(self) -> None:
        event = MessageReceived(body="hello")
        assert isinstance(event.id, UUID)
        assert isinstance(event.timestamp, datetime)
        assert event.timestamp.tzinfo is not None

    def test_events_are_immutable(self) -> None:
        event = MessageReceived(body="hello")
        with pytest.raises(Exception):  # noqa: B017, PT011
            event.body = "mutated"  # type: ignore[misc]

    def test_message_received_defaults(self) -> None:
        event = MessageReceived(body="hi")
        assert event.role == "user"
        assert event.trust == "owner"
        assert event.meta == {}
        assert event.durable is True

    def test_response_complete_defaults(self) -> None:
        event = ResponseComplete(body="reply")
        assert event.role == "assistant"
        assert event.episode_id is None
        assert event.durable is True

    def test_system_event(self) -> None:
        event = SystemEvent(kind="startup", detail="ready")
        assert event.kind == "startup"
        assert event.durable is True

    def test_response_chunk_is_ephemeral(self) -> None:
        chunk = ResponseChunk(body="tok")
        assert chunk.durable is False

    def test_explicit_id_preserved(self) -> None:
        uid = uuid4()
        event = MessageReceived(id=uid, body="hi")
        assert event.id == uid

    def test_session_id_and_channel(self) -> None:
        sid = uuid4()
        event = MessageReceived(body="hi", session_id=sid, channel="message")
        assert event.session_id == sid
        assert event.channel == "message"

    def test_serialization_roundtrip(self) -> None:
        """Events survive JSON serialization and deserialization."""
        original = MessageReceived(
            body="round trip",
            role="user",
            trust="verified",
            channel="message",
            meta={"key": "value"},
        )
        json_str = original.model_dump_json()
        restored = MessageReceived.model_validate_json(json_str)

        assert restored.id == original.id
        assert restored.body == original.body
        assert restored.timestamp == original.timestamp
        assert restored.trust == "verified"
        assert restored.meta == {"key": "value"}


# ── EventBus tests ───────────────────────────────────────────────────


def _mock_pool() -> MagicMock:
    """Create a mock asyncpg pool with async execute/fetch."""
    pool = MagicMock()
    pool.execute = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    return pool


@pytest.fixture
def mock_db():
    """Patch the db singleton to use a mock pool."""
    pool = _mock_pool()
    with patch("theo.bus.core.db") as mock:
        mock.pool = pool
        yield pool


class TestEventBus:
    async def test_publish_raises_when_not_started(self) -> None:
        event_bus = EventBus()
        with pytest.raises(BusNotRunningError):
            await event_bus.publish(MessageReceived(body="hello"))

    async def test_single_handler_dispatch(self, mock_db: MagicMock) -> None:
        _ = mock_db  # fixture provides patched db
        event_bus = EventBus()
        received: list[MessageReceived] = []

        async def handler(event: MessageReceived) -> None:
            received.append(event)

        event_bus.subscribe(MessageReceived, handler)
        await event_bus.start()

        event = MessageReceived(body="hello")
        await event_bus.publish(event)

        await event_bus.stop()
        assert len(received) == 1
        assert received[0].body == "hello"

    async def test_multi_handler_dispatch(self, mock_db: MagicMock) -> None:
        _ = mock_db
        event_bus = EventBus()
        results: list[str] = []

        async def handler_a(_event: MessageReceived) -> None:
            results.append("a")

        async def handler_b(_event: MessageReceived) -> None:
            results.append("b")

        event_bus.subscribe(MessageReceived, handler_a)
        event_bus.subscribe(MessageReceived, handler_b)
        await event_bus.start()

        await event_bus.publish(MessageReceived(body="hi"))
        await event_bus.stop()

        assert sorted(results) == ["a", "b"]

    async def test_handler_error_isolation(self, mock_db: MagicMock) -> None:
        """A failing handler must not prevent other handlers from running."""
        _ = mock_db
        event_bus = EventBus()
        results: list[str] = []

        async def bad_handler(_event: MessageReceived) -> None:
            msg = "boom"
            raise RuntimeError(msg)

        async def good_handler(event: MessageReceived) -> None:
            results.append(event.body)

        event_bus.subscribe(MessageReceived, bad_handler)
        event_bus.subscribe(MessageReceived, good_handler)
        await event_bus.start()

        await event_bus.publish(MessageReceived(body="test"))
        await event_bus.stop()

        assert results == ["test"]

    async def test_handler_failure_leaves_event_unprocessed(self, mock_db: MagicMock) -> None:
        """When a handler fails, the durable event must NOT be marked processed."""
        event_bus = EventBus()

        async def failing_handler(_event: MessageReceived) -> None:
            msg = "fail"
            raise RuntimeError(msg)

        event_bus.subscribe(MessageReceived, failing_handler)
        await event_bus.start()

        await event_bus.publish(MessageReceived(body="will fail"))
        await event_bus.stop()

        # _persist was called (INSERT), but _MARK_PROCESSED should NOT have been called.
        insert_calls = [c for c in mock_db.execute.call_args_list if "INSERT" in str(c)]
        update_calls = [c for c in mock_db.execute.call_args_list if "UPDATE" in str(c)]
        assert len(insert_calls) == 1
        assert len(update_calls) == 0

    async def test_durable_event_persisted_before_dispatch(self, mock_db: MagicMock) -> None:
        event_bus = EventBus()
        call_order: list[str] = []

        original_execute = mock_db.execute

        async def tracking_execute(*args: object, **kwargs: object) -> None:
            if "INSERT" in str(args[0]):
                call_order.append("persist")
            elif "UPDATE" in str(args[0]):
                call_order.append("mark_processed")
            await original_execute(*args, **kwargs)

        mock_db.execute = AsyncMock(side_effect=tracking_execute)

        async def handler(_event: MessageReceived) -> None:
            call_order.append("dispatch")

        event_bus.subscribe(MessageReceived, handler)
        await event_bus.start()

        await event_bus.publish(MessageReceived(body="order test"))
        await event_bus.stop()

        assert call_order == ["persist", "dispatch", "mark_processed"]

    async def test_ephemeral_event_skips_persistence(self, mock_db: MagicMock) -> None:
        _ = mock_db
        event_bus = EventBus()
        received: list[ResponseChunk] = []

        async def handler(event: ResponseChunk) -> None:
            received.append(event)

        event_bus.subscribe(ResponseChunk, handler)
        await event_bus.start()

        await event_bus.publish(ResponseChunk(body="tok", done=False))
        await event_bus.stop()

        assert len(received) == 1
        # No INSERT or UPDATE calls for ephemeral events.
        mock_db.execute.assert_not_called()

    async def test_replay_on_start(self, mock_db: MagicMock) -> None:
        """Unprocessed rows from the DB are replayed to handlers on start."""
        event = MessageReceived(body="replayed")
        mock_db.fetch = AsyncMock(
            return_value=[
                {
                    "id": event.id,
                    "type": "MessageReceived",
                    "payload": event.model_dump_json(),
                    "session_id": None,
                    "channel": None,
                    "created_at": event.timestamp,
                },
            ]
        )

        event_bus = EventBus()
        received: list[MessageReceived] = []

        async def handler(e: MessageReceived) -> None:
            received.append(e)

        event_bus.subscribe(MessageReceived, handler)
        await event_bus.start()
        # Give the dispatch loop time to process the replayed event.
        await asyncio.sleep(0.05)
        await event_bus.stop()

        assert len(received) == 1
        assert received[0].body == "replayed"

    async def test_event_type_isolation(self, mock_db: MagicMock) -> None:
        """Handlers only receive events of their subscribed type."""
        _ = mock_db
        event_bus = EventBus()
        msg_received: list[MessageReceived] = []
        sys_received: list[SystemEvent] = []

        async def msg_handler(event: MessageReceived) -> None:
            msg_received.append(event)

        async def sys_handler(event: SystemEvent) -> None:
            sys_received.append(event)

        event_bus.subscribe(MessageReceived, msg_handler)
        event_bus.subscribe(SystemEvent, sys_handler)
        await event_bus.start()

        await event_bus.publish(MessageReceived(body="msg"))
        await event_bus.publish(SystemEvent(kind="startup"))
        await event_bus.stop()

        assert len(msg_received) == 1
        assert len(sys_received) == 1

    async def test_stop_is_idempotent(self, mock_db: MagicMock) -> None:
        _ = mock_db
        event_bus = EventBus()
        await event_bus.start()
        await event_bus.stop()
        await event_bus.stop()  # should not raise

    async def test_start_is_idempotent(self, mock_db: MagicMock) -> None:
        _ = mock_db
        event_bus = EventBus()
        await event_bus.start()
        await event_bus.start()  # should not raise or create duplicate tasks
        await event_bus.stop()

    async def test_mark_processed_failure_does_not_kill_loop(self, mock_db: MagicMock) -> None:
        """A DB error when marking processed must not crash the dispatch loop."""
        event_bus = EventBus()
        received: list[str] = []
        call_count = 0

        original_execute = mock_db.execute

        async def flaky_execute(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            # Fail on the first MARK_PROCESSED call.
            if "UPDATE" in str(args[0]) and call_count <= 2:
                msg = "connection lost"
                raise OSError(msg)
            await original_execute(*args, **kwargs)

        mock_db.execute = AsyncMock(side_effect=flaky_execute)

        async def handler(event: MessageReceived) -> None:
            received.append(event.body)

        event_bus.subscribe(MessageReceived, handler)
        await event_bus.start()

        await event_bus.publish(MessageReceived(body="first"))
        await event_bus.publish(MessageReceived(body="second"))
        await event_bus.stop()

        # Both events should have been dispatched despite the DB error.
        assert received == ["first", "second"]

    async def test_replay_skips_unknown_event_types(self, mock_db: MagicMock) -> None:
        """Unknown event types in the DB are skipped during replay."""
        mock_db.fetch = AsyncMock(
            return_value=[
                {
                    "id": uuid4(),
                    "type": "NoSuchEvent",
                    "payload": "{}",
                    "session_id": None,
                    "channel": None,
                    "created_at": datetime.now(UTC),
                },
            ]
        )

        event_bus = EventBus()
        received: list[MessageReceived] = []

        async def handler(e: MessageReceived) -> None:
            received.append(e)

        event_bus.subscribe(MessageReceived, handler)
        await event_bus.start()
        await asyncio.sleep(0.05)
        await event_bus.stop()

        assert len(received) == 0
