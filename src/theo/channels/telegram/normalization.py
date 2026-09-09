"""Normalize Telegram message payloads into text and attachment descriptors.

Pure transformations preserve original media references and reply metadata;
network downloads and durable admission belong to other Telegram modules.
"""

from typing import Any

from aiogram.types import Message

from theo.domain import Json, encode


def message_text(message: Message) -> str:
    text = message.text or message.caption or ""
    rich = getattr(message, "rich_message", None)
    if rich:
        text += "\nRich content (untrusted): " + encode(rich.model_dump(exclude_none=True))[:50000]
    return text


def normalize(message: Message) -> Json:
    text = message_text(message)
    parts: list[Json] = []
    for kind in (
        "voice",
        "audio",
        "document",
        "video",
        "animation",
        "sticker",
        "video_note",
        "photo",
    ):
        media: Any = getattr(message, kind, None)
        if not media:
            continue
        if kind == "photo":
            media = media[-1]
        parts.append(
            {
                "kind": kind,
                "metadata": {
                    "file_id": media.file_id,
                    "size": media.file_size,
                    "name": getattr(media, "file_name", None),
                    "mime": getattr(media, "mime_type", None),
                    "animated": getattr(media, "is_animated", False),
                    "video": getattr(media, "is_video", False),
                    "duration": getattr(media, "duration", None),
                    "state": "pending",
                },
            }
        )
        break
    for kind in ("venue", "location", "contact"):
        value = getattr(message, kind, None)
        if value:
            parts.append(
                {"kind": kind, "metadata": value.model_dump(mode="json", exclude_none=True)}
            )
            break
    reference: Json | None = None
    if message.reply_to_message:
        reply = message.reply_to_message
        reference = {
            "message_id": reply.message_id,
            "chat_id": reply.chat.id,
            "topic_id": reply.message_thread_id or 0,
            "sender_id": reply.from_user.id if reply.from_user else None,
            "text": (message_text(reply) or "[Referenced media]")[:8000],
        }
    elif message.external_reply:
        reference = {
            "unavailable": True,
            "reference": message.external_reply.model_dump(mode="json", exclude_none=True),
        }
    if message.quote:
        reference = {**(reference or {}), "quote": message.quote.text[:8000]}
    return {
        "text": text
        or ("[Owner sent " + ", ".join(p["kind"] for p in parts) + "]" if parts else ""),
        "parts": parts,
        "reply": reference,
        "forward": message.forward_origin.model_dump(mode="json", exclude_none=True)
        if message.forward_origin
        else None,
    }
