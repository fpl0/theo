"""Tests for the Telegram gate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from aiogram.exceptions import TelegramAPIError

from theo.bus import EventBus, bus
from theo.bus.events import MessageReceived, ResponseChunk, ResponseComplete
from theo.config import Settings
from theo.conversation import ConversationEngine
from theo.errors import GateConfigError, TranscriptionError
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


def _make_gate(engine: ConversationEngine | None = None) -> TelegramGate:
    """Create a gate with mocked settings and bot."""
    settings = _settings(telegram_bot_token=_TEST_TOKEN, telegram_owner_chat_id=42)
    with patch("theo.gates.telegram.get_settings", return_value=settings):
        return TelegramGate(engine=engine)


def _make_message(chat_id: int, text: str | None = "hello") -> MagicMock:
    """Create a mock aiogram Message."""
    msg = MagicMock()
    msg.chat.id = chat_id
    msg.text = text
    msg.message_id = 100
    msg.voice = None
    msg.audio = None
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
        gate._bot.send_chat_action = AsyncMock()

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

        gate._bot.edit_message_text.assert_called_once()
        call_text = gate._bot.edit_message_text.call_args.kwargs["text"]
        assert call_text == escape_markdownv2("final answer")
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


# ── Command handler tests ─────────────────────────────────────────────


def _make_cmd_message(chat_id: int, text: str) -> MagicMock:
    """Create a mock aiogram Message for command testing."""
    msg = MagicMock()
    msg.chat.id = chat_id
    msg.text = text
    msg.answer = AsyncMock()
    msg.message_id = 100
    return msg


class TestCommandPermissions:
    """All commands must be owner-only."""

    @pytest.mark.parametrize(
        "handler",
        [
            "_on_cmd_start",
            "_on_cmd_pause",
            "_on_cmd_resume",
            "_on_cmd_stop",
            "_on_cmd_kill",
            "_on_cmd_status",
            "_on_cmd_onboard",
        ],
    )
    async def test_non_owner_gets_no_response(self, handler: str) -> None:
        gate = _make_gate()
        msg = _make_cmd_message(chat_id=999, text="/start")
        await getattr(gate, handler)(msg)
        msg.answer.assert_not_called()


class TestStartCommand:
    async def test_sends_welcome_message(self) -> None:
        gate = _make_gate()
        msg = _make_cmd_message(chat_id=42, text="/start")
        await gate._on_cmd_start(msg)
        msg.answer.assert_called_once()
        text = msg.answer.call_args.args[0]
        assert "Theo" in text

    async def test_uses_plain_text(self) -> None:
        gate = _make_gate()
        msg = _make_cmd_message(chat_id=42, text="/start")
        await gate._on_cmd_start(msg)
        assert msg.answer.call_args.kwargs.get("parse_mode") is None


class TestPauseCommand:
    async def test_pauses_engine(self) -> None:
        engine = ConversationEngine()
        engine._state = "running"
        gate = _make_gate(engine=engine)
        msg = _make_cmd_message(chat_id=42, text="/pause")
        await gate._on_cmd_pause(msg)
        assert engine.state == "paused"
        msg.answer.assert_called_once()
        assert "Paused" in msg.answer.call_args.args[0]

    async def test_works_without_engine(self) -> None:
        gate = _make_gate()
        msg = _make_cmd_message(chat_id=42, text="/pause")
        await gate._on_cmd_pause(msg)
        msg.answer.assert_called_once()


class TestResumeCommand:
    async def test_resumes_engine(self) -> None:
        engine = ConversationEngine()
        engine._state = "paused"
        gate = _make_gate(engine=engine)
        msg = _make_cmd_message(chat_id=42, text="/resume")
        await gate._on_cmd_resume(msg)
        assert engine.state == "running"
        msg.answer.assert_called_once()
        assert "Resumed" in msg.answer.call_args.args[0]


class TestStopCommand:
    async def test_stops_engine_after_drain(self) -> None:
        engine = ConversationEngine()
        engine._state = "running"
        gate = _make_gate(engine=engine)
        msg = _make_cmd_message(chat_id=42, text="/stop")
        await gate._on_cmd_stop(msg)
        assert engine.state == "stopped"
        msg.answer.assert_called_once()
        assert "Stopping" in msg.answer.call_args.args[0]

    async def test_works_without_engine(self) -> None:
        gate = _make_gate()
        msg = _make_cmd_message(chat_id=42, text="/stop")
        await gate._on_cmd_stop(msg)
        msg.answer.assert_called_once()
        assert "Stopped" in msg.answer.call_args.args[0]


class TestKillCommand:
    async def test_kills_engine_immediately(self) -> None:
        engine = ConversationEngine()
        engine._state = "running"
        gate = _make_gate(engine=engine)
        msg = _make_cmd_message(chat_id=42, text="/kill")
        await gate._on_cmd_kill(msg)
        assert engine.state == "stopped"
        msg.answer.assert_called_once()
        assert "Killed" in msg.answer.call_args.args[0]

    async def test_kill_sets_drained(self) -> None:
        engine = ConversationEngine()
        engine._state = "running"
        engine._drained.clear()
        gate = _make_gate(engine=engine)
        msg = _make_cmd_message(chat_id=42, text="/kill")
        await gate._on_cmd_kill(msg)
        assert engine._drained.is_set()


class TestStatusCommand:
    async def test_reports_engine_state(self) -> None:
        engine = ConversationEngine()
        engine._state = "running"
        gate = _make_gate(engine=engine)
        gate._start_time = 0.0  # force a known uptime
        msg = _make_cmd_message(chat_id=42, text="/status")
        await gate._on_cmd_status(msg)
        msg.answer.assert_called_once()
        text = msg.answer.call_args.args[0]
        assert "Engine: running" in text
        assert "In-flight: 0" in text
        assert "Queue: 0" in text
        assert "Uptime:" in text

    async def test_reports_queue_depth(self) -> None:
        engine = ConversationEngine()
        engine._state = "paused"
        engine._paused_queue.put_nowait(MagicMock())
        engine._paused_queue.put_nowait(MagicMock())
        gate = _make_gate(engine=engine)
        msg = _make_cmd_message(chat_id=42, text="/status")
        await gate._on_cmd_status(msg)
        text = msg.answer.call_args.args[0]
        assert "Queue: 2" in text

    async def test_no_engine_shows_minimal_status(self) -> None:
        gate = _make_gate()
        msg = _make_cmd_message(chat_id=42, text="/status")
        await gate._on_cmd_status(msg)
        text = msg.answer.call_args.args[0]
        assert "No status available" in text


class TestCommandStateTransitions:
    """Verify full state transition sequences via commands."""

    async def test_pause_resume_cycle(self) -> None:
        engine = ConversationEngine()
        engine._state = "running"
        gate = _make_gate(engine=engine)

        msg = _make_cmd_message(chat_id=42, text="/pause")
        await gate._on_cmd_pause(msg)
        assert engine.state == "paused"

        msg = _make_cmd_message(chat_id=42, text="/resume")
        await gate._on_cmd_resume(msg)
        assert engine.state == "running"

    async def test_stop_from_running(self) -> None:
        engine = ConversationEngine()
        engine._state = "running"
        gate = _make_gate(engine=engine)

        msg = _make_cmd_message(chat_id=42, text="/stop")
        await gate._on_cmd_stop(msg)
        assert engine.state == "stopped"

    async def test_kill_from_paused(self) -> None:
        engine = ConversationEngine()
        engine._state = "paused"
        gate = _make_gate(engine=engine)

        msg = _make_cmd_message(chat_id=42, text="/kill")
        await gate._on_cmd_kill(msg)
        assert engine.state == "stopped"


# ── Voice message tests ────────────────────────────────────────────


def _make_voice_message(chat_id: int, duration: int = 5) -> MagicMock:
    """Create a mock aiogram Message with a voice attachment."""
    msg = MagicMock()
    msg.chat.id = chat_id
    msg.text = None
    msg.message_id = 100
    msg.voice = MagicMock()
    msg.voice.file_id = "file_abc123"
    msg.voice.duration = duration
    msg.audio = None
    msg.reply = AsyncMock()
    return msg


def _setup_voice_bot(gate: TelegramGate, file_path: str = "voice/file.ogg") -> None:
    """Mock bot methods needed for voice message handling."""
    file_mock = MagicMock()
    file_mock.file_path = file_path
    gate._bot.get_file = AsyncMock(return_value=file_mock)
    gate._bot.download_file = AsyncMock()
    gate._bot.send_chat_action = AsyncMock()


class TestVoiceMessageHandling:
    async def test_voice_message_publishes_transcribed_text(self) -> None:
        gate = _make_gate()
        _setup_voice_bot(gate)
        published: list[MessageReceived] = []

        async def mock_publish(event: object) -> None:
            if isinstance(event, MessageReceived):
                published.append(event)

        with (
            patch.object(bus, "publish", side_effect=mock_publish),
            patch("theo.gates.telegram.transcriber") as mock_transcriber,
        ):
            mock_transcriber.transcribe = AsyncMock(return_value="hello from voice")
            await gate._on_message(_make_voice_message(chat_id=42))

        assert len(published) == 1
        assert published[0].body == "hello from voice"
        assert published[0].channel == "message"
        assert published[0].trust == "owner"
        assert published[0].meta["source"] == "voice"
        assert published[0].meta["duration_s"] == 5

    async def test_voice_from_non_owner_ignored(self) -> None:
        gate = _make_gate()
        published: list[object] = []

        async def mock_publish(event: object) -> None:
            published.append(event)

        with patch.object(bus, "publish", side_effect=mock_publish):
            await gate._on_message(_make_voice_message(chat_id=999))

        assert len(published) == 0

    async def test_empty_transcription_not_published(self) -> None:
        gate = _make_gate()
        _setup_voice_bot(gate)
        published: list[object] = []

        async def mock_publish(event: object) -> None:
            published.append(event)

        with (
            patch.object(bus, "publish", side_effect=mock_publish),
            patch("theo.gates.telegram.transcriber") as mock_transcriber,
        ):
            mock_transcriber.transcribe = AsyncMock(return_value="")
            await gate._on_message(_make_voice_message(chat_id=42))

        assert len(published) == 0

    async def test_voice_meta_includes_telegram_ids(self) -> None:
        gate = _make_gate()
        _setup_voice_bot(gate)
        published: list[MessageReceived] = []

        async def mock_publish(event: object) -> None:
            if isinstance(event, MessageReceived):
                published.append(event)

        with (
            patch.object(bus, "publish", side_effect=mock_publish),
            patch("theo.gates.telegram.transcriber") as mock_transcriber,
        ):
            mock_transcriber.transcribe = AsyncMock(return_value="transcribed")
            msg = _make_voice_message(chat_id=42)
            msg.message_id = 777
            await gate._on_message(msg)

        assert published[0].meta["telegram_chat_id"] == 42
        assert published[0].meta["telegram_message_id"] == 777

    async def test_temp_file_cleaned_up_after_transcription(self) -> None:
        gate = _make_gate()
        _setup_voice_bot(gate)

        unlinked: list[str] = []

        def tracking_unlink(self: Path, **_kwargs: object) -> None:
            unlinked.append(str(self))

        with (
            patch.object(bus, "publish", new_callable=AsyncMock),
            patch("theo.gates.telegram.transcriber") as mock_transcriber,
            patch.object(Path, "unlink", tracking_unlink),
        ):
            mock_transcriber.transcribe = AsyncMock(return_value="text")
            await gate._on_message(_make_voice_message(chat_id=42))

        assert len(unlinked) == 1
        assert unlinked[0].endswith(".ogg")

    async def test_temp_file_cleaned_up_on_transcription_error(self) -> None:
        gate = _make_gate()
        _setup_voice_bot(gate)

        unlinked: list[str] = []

        def tracking_unlink(self: Path, **_kwargs: object) -> None:
            unlinked.append(str(self))

        published: list[object] = []

        async def mock_publish(event: object) -> None:
            published.append(event)

        with (
            patch.object(bus, "publish", side_effect=mock_publish),
            patch("theo.gates.telegram.transcriber") as mock_transcriber,
            patch.object(Path, "unlink", tracking_unlink),
        ):
            mock_transcriber.transcribe = AsyncMock(
                side_effect=TranscriptionError("boom"),
            )
            await gate._on_message(_make_voice_message(chat_id=42))

        assert len(unlinked) == 1
        assert len(published) == 0

    async def test_audio_message_handled_like_voice(self) -> None:
        gate = _make_gate()
        _setup_voice_bot(gate, file_path="audio/file.mp3")
        published: list[MessageReceived] = []

        async def mock_publish(event: object) -> None:
            if isinstance(event, MessageReceived):
                published.append(event)

        msg = MagicMock()
        msg.chat.id = 42
        msg.text = None
        msg.message_id = 100
        msg.voice = None
        msg.audio = MagicMock()
        msg.audio.file_id = "audio_file_123"
        msg.audio.duration = 10
        msg.reply = AsyncMock()

        with (
            patch.object(bus, "publish", side_effect=mock_publish),
            patch("theo.gates.telegram.transcriber") as mock_transcriber,
        ):
            mock_transcriber.transcribe = AsyncMock(return_value="audio text")
            await gate._on_message(msg)

        assert len(published) == 1
        assert published[0].body == "audio text"
        assert published[0].meta["source"] == "voice"
        assert published[0].meta["duration_s"] == 10

    async def test_voice_sends_typing_indicator(self) -> None:
        gate = _make_gate()
        _setup_voice_bot(gate)

        with (
            patch.object(bus, "publish", new_callable=AsyncMock),
            patch("theo.gates.telegram.transcriber") as mock_transcriber,
        ):
            mock_transcriber.transcribe = AsyncMock(return_value="text")
            await gate._on_message(_make_voice_message(chat_id=42))

        gate._bot.send_chat_action.assert_called_once()

    async def test_transcription_error_replies_to_user(self) -> None:
        gate = _make_gate()
        _setup_voice_bot(gate)

        msg = _make_voice_message(chat_id=42)

        with (
            patch.object(bus, "publish", new_callable=AsyncMock),
            patch("theo.gates.telegram.transcriber") as mock_transcriber,
            patch.object(Path, "unlink", lambda _self, **_kw: None),
        ):
            mock_transcriber.transcribe = AsyncMock(
                side_effect=TranscriptionError("failed"),
            )
            await gate._on_message(msg)

        msg.reply.assert_called_once()
        assert "couldn't transcribe" in msg.reply.call_args.args[0]

    async def test_voice_download_error_replies_to_user(self) -> None:
        gate = _make_gate()
        gate._bot.get_file = AsyncMock(side_effect=RuntimeError("network"))
        gate._bot.send_chat_action = AsyncMock()

        msg = _make_voice_message(chat_id=42)

        with patch.object(bus, "publish", new_callable=AsyncMock):
            await gate._on_message(msg)

        msg.reply.assert_called_once()
        assert "went wrong" in msg.reply.call_args.args[0]

    async def test_voice_file_path_none_replies_to_user(self) -> None:
        gate = _make_gate()
        _setup_voice_bot(gate)
        # Override file_path to None.
        gate._bot.get_file = AsyncMock(return_value=MagicMock(file_path=None))

        msg = _make_voice_message(chat_id=42)

        with patch.object(bus, "publish", new_callable=AsyncMock):
            await gate._on_message(msg)

        msg.reply.assert_called_once()
        assert "download" in msg.reply.call_args.args[0].lower()


# ── Response error handling tests ──────────────────────────────────


def _telegram_error(msg: str = "error") -> TelegramAPIError:
    """Create a TelegramAPIError for testing."""
    return TelegramAPIError(method=MagicMock(), message=msg)


class TestResponseErrorHandling:
    async def test_chunk_send_failure_does_not_propagate(self) -> None:
        gate = _make_gate()
        session_id = session_id_for_chat(42)

        gate._bot.send_chat_action = AsyncMock()
        gate._bot.send_message = AsyncMock(side_effect=_telegram_error("rate limit"))

        chunk = ResponseChunk(body="hello", session_id=session_id, done=False)
        # Should not raise.
        await gate._on_response_chunk(chunk)

        assert session_id not in gate._streaming

    async def test_chunk_edit_failure_does_not_propagate(self) -> None:
        gate = _make_gate()
        session_id = session_id_for_chat(42)
        gate._streaming[session_id] = 200

        gate._bot.edit_message_text = AsyncMock(side_effect=_telegram_error("deleted"))

        chunk = ResponseChunk(body="updated", session_id=session_id, done=False)
        # Should not raise.
        await gate._on_response_chunk(chunk)

    async def test_complete_edit_failure_falls_back_to_send(self) -> None:
        gate = _make_gate()
        session_id = session_id_for_chat(42)
        gate._streaming[session_id] = 200

        gate._bot.edit_message_text = AsyncMock(side_effect=_telegram_error("deleted"))
        gate._bot.send_message = AsyncMock()

        event = ResponseComplete(body="final answer", session_id=session_id)
        await gate._on_response_complete(event)

        gate._bot.send_message.assert_called_once()
        assert session_id not in gate._streaming

    async def test_first_chunk_sends_typing_indicator(self) -> None:
        gate = _make_gate()
        session_id = session_id_for_chat(42)

        sent = MagicMock()
        sent.message_id = 200
        gate._bot.send_message = AsyncMock(return_value=sent)
        gate._bot.send_chat_action = AsyncMock()

        chunk = ResponseChunk(body="hello", session_id=session_id, done=False)
        await gate._on_response_chunk(chunk)

        gate._bot.send_chat_action.assert_called_once()

    async def test_split_respects_escaped_length(self) -> None:
        """Splitting happens after escaping, so parts stay within Telegram's limit."""
        from theo.gates.telegram import _MAX_MESSAGE_LENGTH

        # Create text with special chars that will expand when escaped.
        raw = "_" * 2100  # each becomes \_ (2 chars), so 4200 escaped chars
        escaped = escape_markdownv2(raw)
        parts = split_message(escaped, max_length=_MAX_MESSAGE_LENGTH)
        assert all(len(p) <= _MAX_MESSAGE_LENGTH for p in parts)


# ── Onboard command tests ─────────────────────────────────────────


class TestOnboardCommand:
    async def test_non_owner_blocked(self) -> None:
        gate = _make_gate()
        msg = _make_cmd_message(chat_id=999, text="/onboard")
        await gate._on_cmd_onboard(msg)
        msg.answer.assert_not_called()
