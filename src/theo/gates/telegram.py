"""Telegram gate — converts Telegram messages to bus events and back."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from opentelemetry import metrics, trace

from theo.bus import bus
from theo.bus.events import MessageReceived, ResponseChunk, ResponseComplete
from theo.config import get_settings
from theo.errors import GateConfigError

if TYPE_CHECKING:
    from aiogram.types import Message

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

    def __init__(self) -> None:
        cfg = get_settings()
        if cfg.telegram_bot_token is None:
            msg = "THEO_TELEGRAM_BOT_TOKEN is required"
            raise GateConfigError(msg)
        if cfg.telegram_owner_chat_id is None:
            msg = "THEO_TELEGRAM_OWNER_CHAT_ID is required"
            raise GateConfigError(msg)

        self._owner_chat_id: int = cfg.telegram_owner_chat_id
        self._bot = Bot(
            token=cfg.telegram_bot_token.get_secret_value(),
            default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
        )
        self._dp = Dispatcher()
        self._dp.message.register(self._on_message)

        # Track the in-flight streamed message per session so chunks can edit it.
        self._streaming: dict[UUID, int] = {}  # session_id → telegram message_id
        self._polling_task: asyncio.Task[None] | None = None

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Subscribe to bus events and start polling Telegram."""
        bus.subscribe(ResponseChunk, self._on_response_chunk)
        bus.subscribe(ResponseComplete, self._on_response_complete)
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

    # ── inbound: Telegram → bus ──────────────────────────────────────

    async def _on_message(self, message: Message) -> None:
        """Handle an incoming Telegram message."""
        chat_id = message.chat.id

        if chat_id != self._owner_chat_id:
            _messages_ignored_counter.add(1)
            log.warning(
                "ignored message from non-owner",
                extra={"chat_id": chat_id},
            )
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
