"""Telegram gate — converts Telegram messages to bus events and back."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from opentelemetry import metrics, trace

from theo.bus import bus
from theo.bus.events import MessageReceived, ResponseChunk, ResponseComplete
from theo.config import get_settings
from theo.errors import GateConfigError, TranscriptionError
from theo.transcription import transcriber

if TYPE_CHECKING:
    from aiogram.types import Message

    from theo.conversation import ConversationEngine

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

_messages_received_counter = meter.create_counter(
    "theo.telegram.messages_received",
    description="Total messages received from Telegram",
)
_messages_sent_counter = meter.create_counter(
    "theo.telegram.messages_sent",
    description="Total messages sent to Telegram",
)
_messages_ignored_counter = meter.create_counter(
    "theo.telegram.messages_ignored",
    description="Total messages ignored (non-owner)",
)
_voice_received_counter = meter.create_counter(
    "theo.telegram.voice_received",
    description="Total voice messages received and transcribed",
)
_commands_counter = meter.create_counter(
    "theo.telegram.commands",
    description="Total slash commands received",
)

# Namespace for deriving stable session UUIDs from chat IDs.
_TELEGRAM_SESSION_NS = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")

# Telegram message length limit.
_MAX_MESSAGE_LENGTH = 4096

# MarkdownV2 special characters that must be escaped.
_MARKDOWNV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_markdownv2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2 formatting."""
    return _MARKDOWNV2_ESCAPE_RE.sub(r"\\\1", text)


def split_message(text: str, max_length: int = _MAX_MESSAGE_LENGTH) -> list[str]:
    """Split *text* into chunks of at most *max_length* characters.

    Splits on the last newline before the limit when possible,
    otherwise hard-splits at *max_length*.
    """
    if len(text) <= max_length:
        return [text]

    parts: list[str] = []
    while text:
        if len(text) <= max_length:
            parts.append(text)
            break

        # Try to split on the last newline within the limit.
        cut = text.rfind("\n", 0, max_length)
        if cut <= 0:
            cut = max_length

        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")

    return parts


def session_id_for_chat(chat_id: int) -> UUID:
    """Derive a deterministic session UUID from a Telegram chat ID."""
    return uuid5(_TELEGRAM_SESSION_NS, str(chat_id))


class TelegramGate:
    """Bridges Telegram ↔ Theo event bus.

    Incoming owner messages become :class:`MessageReceived` events.
    :class:`ResponseChunk` and :class:`ResponseComplete` events are
    delivered back as Telegram messages (streamed via edit).
    """

    def __init__(self, engine: ConversationEngine | None = None) -> None:
        cfg = get_settings()
        if cfg.telegram_bot_token is None:
            msg = "THEO_TELEGRAM_BOT_TOKEN is required"
            raise GateConfigError(msg)
        if cfg.telegram_owner_chat_id is None:
            msg = "THEO_TELEGRAM_OWNER_CHAT_ID is required"
            raise GateConfigError(msg)

        self._owner_chat_id: int = cfg.telegram_owner_chat_id
        self._engine = engine
        self._bot = Bot(
            token=cfg.telegram_bot_token.get_secret_value(),
            default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
        )
        self._dp = Dispatcher()

        # Register command handlers before the catch-all message handler.
        self._dp.message.register(self._on_cmd_start, Command("start"))
        self._dp.message.register(self._on_cmd_pause, Command("pause"))
        self._dp.message.register(self._on_cmd_resume, Command("resume"))
        self._dp.message.register(self._on_cmd_stop, Command("stop"))
        self._dp.message.register(self._on_cmd_kill, Command("kill"))
        self._dp.message.register(self._on_cmd_status, Command("status"))
        self._dp.message.register(self._on_message)

        # Track the in-flight streamed message per session so chunks can edit it.
        self._streaming: dict[UUID, int] = {}  # session_id → telegram message_id
        self._polling_task: asyncio.Task[None] | None = None
        self._start_time: float | None = None

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Subscribe to bus events and start polling Telegram."""
        bus.subscribe(ResponseChunk, self._on_response_chunk)
        bus.subscribe(ResponseComplete, self._on_response_complete)
        self._start_time = time.monotonic()
        log.info("telegram gate starting")
        self._polling_task = asyncio.create_task(
            self._dp.start_polling(self._bot, handle_signals=False),
            name="telegram-polling",
        )

    async def stop(self) -> None:
        """Stop polling and close the bot session."""
        log.info("telegram gate stopping")
        if self._polling_task is not None:
            self._polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._polling_task
            self._polling_task = None
        await self._bot.session.close()
        log.info("telegram gate stopped")

    # ── commands ──────────────────────────────────────────────────────

    def _is_owner(self, message: Message) -> bool:
        return message.chat.id == self._owner_chat_id

    async def _on_cmd_start(self, message: Message) -> None:
        """Handle /start — welcome message."""
        if not self._is_owner(message):
            return
        with tracer.start_as_current_span("telegram.command", attributes={"command": "start"}):
            _commands_counter.add(1, {"command": "start"})
            await message.answer(
                "I'm Theo, your personal AI agent. Send me a message to start a conversation.",
                parse_mode=None,
            )

    async def _on_cmd_pause(self, message: Message) -> None:
        """Handle /pause — pause processing, queue incoming messages."""
        if not self._is_owner(message):
            return
        with tracer.start_as_current_span("telegram.command", attributes={"command": "pause"}):
            _commands_counter.add(1, {"command": "pause"})
            if self._engine is not None:
                self._engine.pause()
            await message.answer("Paused. Messages will be queued.", parse_mode=None)
            log.info("owner issued /pause")

    async def _on_cmd_resume(self, message: Message) -> None:
        """Handle /resume — resume processing and drain queue."""
        if not self._is_owner(message):
            return
        with tracer.start_as_current_span("telegram.command", attributes={"command": "resume"}):
            _commands_counter.add(1, {"command": "resume"})
            if self._engine is not None:
                await self._engine.resume()
            await message.answer("Resumed.", parse_mode=None)
            log.info("owner issued /resume")

    async def _on_cmd_stop(self, message: Message) -> None:
        """Handle /stop — finish current turn then idle."""
        if not self._is_owner(message):
            return
        with tracer.start_as_current_span("telegram.command", attributes={"command": "stop"}):
            _commands_counter.add(1, {"command": "stop"})
            if self._engine is not None:
                await message.answer(
                    "Stopping \u2014 waiting for current turn to finish.",
                    parse_mode=None,
                )
                await self._engine.stop()
            else:
                await message.answer("Stopped.", parse_mode=None)
            log.info("owner issued /stop")

    async def _on_cmd_kill(self, message: Message) -> None:
        """Handle /kill — immediately halt all processing."""
        if not self._is_owner(message):
            return
        with tracer.start_as_current_span("telegram.command", attributes={"command": "kill"}):
            _commands_counter.add(1, {"command": "kill"})
            if self._engine is not None:
                self._engine.kill()
            await message.answer("Killed. All processing halted immediately.", parse_mode=None)
            log.info("owner issued /kill")

    async def _on_cmd_status(self, message: Message) -> None:
        """Handle /status — report engine state, uptime, queue depth."""
        if not self._is_owner(message):
            return
        with tracer.start_as_current_span("telegram.command", attributes={"command": "status"}):
            _commands_counter.add(1, {"command": "status"})
            lines: list[str] = []
            if self._engine is not None:
                lines.append(f"Engine: {self._engine.state}")
                lines.append(f"In-flight: {self._engine.inflight}")
                lines.append(f"Queue: {self._engine.queue_depth}")
            if self._start_time is not None:
                uptime_s = int(time.monotonic() - self._start_time)
                hours, remainder = divmod(uptime_s, 3600)
                minutes, seconds = divmod(remainder, 60)
                lines.append(f"Uptime: {hours}h {minutes}m {seconds}s")
            text = "\n".join(lines) if lines else "No status available."
            await message.answer(text, parse_mode=None)
            log.info("owner issued /status")

    # ── inbound: Telegram → bus ──────────────────────────────────────

    async def _on_message(self, message: Message) -> None:
        """Handle an incoming Telegram message (text or voice)."""
        chat_id = message.chat.id

        if chat_id != self._owner_chat_id:
            _messages_ignored_counter.add(1)
            log.warning(
                "ignored message from non-owner",
                extra={"chat_id": chat_id},
            )
            return

        # Voice message path: download, transcribe, publish.
        voice = message.voice or message.audio
        if voice is not None:
            await self._handle_voice(message, chat_id)
            return

        body = message.text
        if not body:
            return

        session_id = session_id_for_chat(chat_id)

        with tracer.start_as_current_span(
            "telegram.message_received",
            attributes={
                "telegram.chat_id": chat_id,
                "session.id": str(session_id),
                "message.length": len(body),
            },
        ):
            _messages_received_counter.add(1)
            log.info(
                "received message",
                extra={"chat_id": chat_id, "session_id": str(session_id)},
            )
            await bus.publish(
                MessageReceived(
                    body=body,
                    session_id=session_id,
                    channel="message",
                    trust="owner",
                    meta={"telegram_chat_id": chat_id, "telegram_message_id": message.message_id},
                ),
            )

    async def _handle_voice(self, message: Message, chat_id: int) -> None:
        """Download a voice/audio message, transcribe it, and publish the text."""
        voice = message.voice or message.audio
        if voice is None:
            return

        session_id = session_id_for_chat(chat_id)
        duration_s: int = getattr(voice, "duration", 0)

        with tracer.start_as_current_span(
            "telegram.voice_received",
            attributes={
                "telegram.chat_id": chat_id,
                "session.id": str(session_id),
                "voice.duration_s": duration_s,
            },
        ) as span:
            tmp: Path | None = None
            try:
                file = await self._bot.get_file(voice.file_id)
                if file.file_path is None:
                    log.warning(
                        "voice file has no download path",
                        extra={"file_id": voice.file_id},
                    )
                    return
                suffix = Path(file.file_path).suffix or ".ogg"
                tmp = Path(tempfile.mkstemp(suffix=suffix)[1])  # noqa: ASYNC230
                await self._bot.download_file(file.file_path, destination=tmp)
                body = await transcriber.transcribe(tmp)
            except TranscriptionError:
                log.exception(
                    "voice transcription failed",
                    extra={"chat_id": chat_id, "file_id": voice.file_id},
                )
                return
            except Exception:
                log.exception(
                    "voice message handling failed",
                    extra={"chat_id": chat_id},
                )
                return
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)  # noqa: ASYNC240

            if not body:
                return

            span.set_attribute("transcription.length", len(body))
            _voice_received_counter.add(1)
            _messages_received_counter.add(1)
            log.info(
                "received voice message",
                extra={
                    "chat_id": chat_id,
                    "session_id": str(session_id),
                    "voice_duration_s": duration_s,
                    "transcription_length": len(body),
                },
            )
            await bus.publish(
                MessageReceived(
                    body=body,
                    session_id=session_id,
                    channel="message",
                    trust="owner",
                    meta={
                        "telegram_chat_id": chat_id,
                        "telegram_message_id": message.message_id,
                        "source": "voice",
                        "duration_s": duration_s,
                    },
                ),
            )

    # ── outbound: bus → Telegram ─────────────────────────────────────

    async def _on_response_chunk(self, event: ResponseChunk) -> None:
        """Stream a response chunk to Telegram (send or edit)."""
        if event.session_id is None:
            return

        with tracer.start_as_current_span(
            "telegram.send_chunk",
            attributes={"session.id": str(event.session_id), "chunk.done": event.done},
        ):
            existing_msg_id = self._streaming.get(event.session_id)

            if existing_msg_id is None:
                # First chunk — send a new message.
                escaped = escape_markdownv2(event.body)
                sent = await self._bot.send_message(
                    chat_id=self._owner_chat_id,
                    text=escaped or escape_markdownv2("..."),
                )
                self._streaming[event.session_id] = sent.message_id
            else:
                # Subsequent chunk — edit the existing message.
                escaped = escape_markdownv2(event.body)
                if escaped:
                    await self._bot.edit_message_text(
                        chat_id=self._owner_chat_id,
                        message_id=existing_msg_id,
                        text=escaped,
                    )

    async def _on_response_complete(self, event: ResponseComplete) -> None:
        """Finalize the response: send the full text, replacing any streamed message."""
        if event.session_id is None:
            return

        with tracer.start_as_current_span(
            "telegram.send_complete",
            attributes={
                "session.id": str(event.session_id),
                "response.length": len(event.body),
            },
        ):
            parts = split_message(event.body)

            existing_msg_id = self._streaming.pop(event.session_id, None)

            for i, part in enumerate(parts):
                escaped = escape_markdownv2(part)
                if i == 0 and existing_msg_id is not None:
                    # Replace the streamed message with final text.
                    await self._bot.edit_message_text(
                        chat_id=self._owner_chat_id,
                        message_id=existing_msg_id,
                        text=escaped,
                    )
                else:
                    await self._bot.send_message(
                        chat_id=self._owner_chat_id,
                        text=escaped,
                    )

            _messages_sent_counter.add(1)
            log.info(
                "delivered response",
                extra={
                    "session_id": str(event.session_id),
                    "parts": len(parts),
                    "length": len(event.body),
                },
            )
