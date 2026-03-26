"""Tests for the Telegram gate."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from theo.bus import EventBus, bus
from theo.bus.events import MessageReceived, ResponseChunk, ResponseComplete
from theo.config import Settings
from theo.errors import GateConfigError
from theo.gates.telegram import (
    _MAX_MESSAGE_LENGTH,
    TelegramGate,
    escape_markdownv2,
    session_id_for_chat,
    split_message,
)

# ── Pure function tests ──────────────────────────────────────────────


class TestEscapeMarkdownV2:
    def test_escapes_special_characters(self) -> None:
        assert escape_markdownv2("hello_world") == r"hello\_world"
        assert escape_markdownv2("a*b*c") == r"a\*b\*c"
        assert escape_markdownv2("1+1=2") == r"1\+1\=2"

    def test_escapes_all_special_chars(self) -> None:
        special = r"_*[]()~`>#+\-=|{}.!"
        escaped = escape_markdownv2(special)
        for char in special:
            if char == "\\":
                assert "\\\\" in escaped
            else:
                assert f"\\{char}" in escaped

    def test_plain_text_unchanged(self) -> None:
        assert escape_markdownv2("hello world 123") == "hello world 123"

    def test_empty_string(self) -> None:
        assert escape_markdownv2("") == ""


class TestSplitMessage:
    def test_short_message_returns_single_part(self) -> None:
        assert split_message("hello") == ["hello"]

    def test_exact_limit_returns_single_part(self) -> None:
        text = "x" * _MAX_MESSAGE_LENGTH
        assert split_message(text) == [text]

    def test_splits_on_newline(self) -> None:
        line = "a" * 100
        text = f"{line}\n{line}\n{line}"
        parts = split_message(text, max_length=210)
        assert len(parts) == 2
        assert parts[0] == f"{line}\n{line}"
        assert parts[1] == line

    def test_hard_split_when_no_newline(self) -> None:
        text = "a" * 300
        parts = split_message(text, max_length=100)
        assert len(parts) == 3
        assert all(len(p) <= 100 for p in parts)
        assert "".join(parts) == text

    def test_empty_string(self) -> None:
        assert split_message("") == [""]

    def test_multiple_splits(self) -> None:
        lines = ["a" * 100 for _ in range(5)]
        text = "\n".join(lines)
        parts = split_message(text, max_length=250)
        assert len(parts) >= 2
        recombined = "\n".join(parts)
        assert recombined == text


class TestSessionIdForChat:
    def test_deterministic(self) -> None:
        a = session_id_for_chat(12345)
        b = session_id_for_chat(12345)
        assert a == b

    def test_different_chat_ids_produce_different_sessions(self) -> None:
        a = session_id_for_chat(111)
        b = session_id_for_chat(222)
        assert a != b

    def test_returns_uuid(self) -> None:
        assert isinstance(session_id_for_chat(1), UUID)


# ── Gate construction tests ──────────────────────────────────────────


_TEST_TOKEN = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"


def _settings(**overrides: object) -> Settings:
    defaults = {"database_url": "postgresql://x:x@localhost/x", "_env_file": None}
    return Settings(**(defaults | overrides))


class TestGateConstruction:
    def test_raises_without_bot_token(self) -> None:
        settings = _settings(telegram_owner_chat_id=123)
        with (
            patch("theo.gates.telegram.get_settings", return_value=settings),
            pytest.raises(GateConfigError, match="BOT_TOKEN"),
        ):
            TelegramGate()

    def test_raises_without_owner_chat_id(self) -> None:
        settings = _settings(telegram_bot_token=_TEST_TOKEN)
        with (
            patch("theo.gates.telegram.get_settings", return_value=settings),
            pytest.raises(GateConfigError, match="OWNER_CHAT_ID"),
        ):
            TelegramGate()


# ── Message handling tests ───────────────────────────────────────────


def _make_gate() -> TelegramGate:
    """Create a gate with mocked settings and bot."""
    settings = _settings(telegram_bot_token=_TEST_TOKEN, telegram_owner_chat_id=42)
    with patch("theo.gates.telegram.get_settings", return_value=settings):
        return TelegramGate()


def _make_message(chat_id: int, text: str | None = "hello") -> MagicMock:
    """Create a mock aiogram Message."""
    msg = MagicMock()
    msg.chat.id = chat_id
    msg.text = text
    msg.message_id = 100
    return msg


class TestMessageHandling:
    async def test_owner_message_publishes_event(self) -> None:
        gate = _make_gate()
        published: list[MessageReceived] = []

        async def mock_publish(event: object) -> None:
            if isinstance(event, MessageReceived):
                published.append(event)

        with patch.object(bus, "publish", side_effect=mock_publish):
            await gate._on_message(_make_message(chat_id=42, text="hi"))

        assert len(published) == 1
        assert published[0].body == "hi"
        assert published[0].channel == "message"
        assert published[0].trust == "owner"
        assert published[0].session_id == session_id_for_chat(42)

    async def test_non_owner_message_ignored(self) -> None:
        gate = _make_gate()
        published: list[object] = []

        async def mock_publish(event: object) -> None:
            published.append(event)

        with patch.object(bus, "publish", side_effect=mock_publish):
            await gate._on_message(_make_message(chat_id=999, text="hi"))

        assert len(published) == 0

    async def test_empty_text_ignored(self) -> None:
        gate = _make_gate()
        published: list[object] = []

        async def mock_publish(event: object) -> None:
            published.append(event)

        with patch.object(bus, "publish", side_effect=mock_publish):
            await gate._on_message(_make_message(chat_id=42, text=None))

        assert len(published) == 0

    async def test_message_meta_includes_telegram_ids(self) -> None:
        gate = _make_gate()
        published: list[MessageReceived] = []

        async def mock_publish(event: object) -> None:
            if isinstance(event, MessageReceived):
                published.append(event)

        with patch.object(bus, "publish", side_effect=mock_publish):
            msg = _make_message(chat_id=42, text="test")
            msg.message_id = 555
            await gate._on_message(msg)

        assert published[0].meta["telegram_chat_id"] == 42
        assert published[0].meta["telegram_message_id"] == 555


# ── Response delivery tests ──────────────────────────────────────────


class TestResponseChunkDelivery:
    async def test_first_chunk_sends_new_message(self) -> None:
        gate = _make_gate()
        session_id = session_id_for_chat(42)

        sent = MagicMock()
        sent.message_id = 200
        gate._bot.send_message = AsyncMock(return_value=sent)
        gate._bot.edit_message_text = AsyncMock()

        chunk = ResponseChunk(body="hello", session_id=session_id, done=False)
        await gate._on_response_chunk(chunk)

        gate._bot.send_message.assert_called_once()
        gate._bot.edit_message_text.assert_not_called()
        assert gate._streaming[session_id] == 200

    async def test_subsequent_chunk_edits_message(self) -> None:
        gate = _make_gate()
        session_id = session_id_for_chat(42)
        gate._streaming[session_id] = 200

        gate._bot.send_message = AsyncMock()
        gate._bot.edit_message_text = AsyncMock()

        chunk = ResponseChunk(body="updated text", session_id=session_id, done=False)
        await gate._on_response_chunk(chunk)

        gate._bot.send_message.assert_not_called()
        gate._bot.edit_message_text.assert_called_once_with(
            chat_id=42,
            message_id=200,
            text=escape_markdownv2("updated text"),
        )

    async def test_chunk_without_session_id_ignored(self) -> None:
        gate = _make_gate()
        gate._bot.send_message = AsyncMock()

        chunk = ResponseChunk(body="orphan")
        await gate._on_response_chunk(chunk)

        gate._bot.send_message.assert_not_called()


class TestResponseCompleteDelivery:
    async def test_complete_edits_streamed_message(self) -> None:
        gate = _make_gate()
        session_id = session_id_for_chat(42)
        gate._streaming[session_id] = 200

        gate._bot.edit_message_text = AsyncMock()
        gate._bot.send_message = AsyncMock()

        event = ResponseComplete(body="final answer", session_id=session_id)
        await gate._on_response_complete(event)

        gate._bot.edit_message_text.assert_called_once_with(
            chat_id=42,
            message_id=200,
            text=escape_markdownv2("final answer"),
        )
        gate._bot.send_message.assert_not_called()
        assert session_id not in gate._streaming

    async def test_complete_sends_new_when_no_stream(self) -> None:
        gate = _make_gate()
        session_id = session_id_for_chat(42)

        gate._bot.send_message = AsyncMock()
        gate._bot.edit_message_text = AsyncMock()

        event = ResponseComplete(body="direct reply", session_id=session_id)
        await gate._on_response_complete(event)

        gate._bot.send_message.assert_called_once()
        gate._bot.edit_message_text.assert_not_called()

    async def test_long_response_splits_into_parts(self) -> None:
        gate = _make_gate()
        session_id = session_id_for_chat(42)

        gate._bot.send_message = AsyncMock()
        gate._bot.edit_message_text = AsyncMock()

        long_body = "x" * (_MAX_MESSAGE_LENGTH + 100)
        event = ResponseComplete(body=long_body, session_id=session_id)
        await gate._on_response_complete(event)

        assert gate._bot.send_message.call_count == 2

    async def test_long_response_edits_first_when_streaming(self) -> None:
        gate = _make_gate()
        session_id = session_id_for_chat(42)
        gate._streaming[session_id] = 300

        gate._bot.edit_message_text = AsyncMock()
        gate._bot.send_message = AsyncMock()

        long_body = "x" * (_MAX_MESSAGE_LENGTH + 100)
        event = ResponseComplete(body=long_body, session_id=session_id)
        await gate._on_response_complete(event)

        gate._bot.edit_message_text.assert_called_once()
        gate._bot.send_message.assert_called_once()

    async def test_complete_without_session_id_ignored(self) -> None:
        gate = _make_gate()
        gate._bot.send_message = AsyncMock()

        event = ResponseComplete(body="orphan")
        await gate._on_response_complete(event)

        gate._bot.send_message.assert_not_called()

    async def test_markdownv2_escaping_applied(self) -> None:
        gate = _make_gate()
        session_id = session_id_for_chat(42)

        gate._bot.send_message = AsyncMock()

        event = ResponseComplete(body="hello_world *bold*", session_id=session_id)
        await gate._on_response_complete(event)

        call_text = gate._bot.send_message.call_args.kwargs["text"]
        assert r"\_" in call_text
        assert r"\*" in call_text


# ── Integration with EventBus ────────────────────────────────────────


class TestBusIntegration:
    async def test_subscribe_registers_handlers(self) -> None:
        """Verify that start() subscribes to the right event types."""
        gate = _make_gate()
        _event_bus = EventBus()
        subscribed_types: list[type] = []

        original_subscribe = _event_bus.subscribe

        def tracking_subscribe(event_type: type, handler: object) -> None:
            subscribed_types.append(event_type)
            original_subscribe(event_type, handler)

        with (
            patch.object(bus, "subscribe", side_effect=tracking_subscribe),
            patch.object(gate._dp, "start_polling", new_callable=AsyncMock),
        ):
            await gate.start()

        assert ResponseChunk in subscribed_types
        assert ResponseComplete in subscribed_types
