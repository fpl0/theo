"""Translate committed outbox operations into Telegram Bot API calls.

Handles uploads, cached file IDs, rich-text fallback and remote receipts. Definite
rejections raise NoEffect; ambiguous network failures remain delivery uncertainty.
"""

import contextlib
from typing import Any, cast

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    InputPollOption,
    InputRichMessage,
    ReactionTypeEmoji,
    ReplyParameters,
)

from theo.config import Settings
from theo.content.artifacts import Artifacts
from theo.delivery.contracts import NoEffect
from theo.domain import Json
from theo.observability import telemetry
from theo.storage import Database


class TelegramSender:
    def __init__(self, db: Database, settings: Settings, bot: Bot, bot_id: int):
        self.db, self.settings, self.bot, self.bot_id = db, settings, bot, bot_id

    @telemetry.observed("telegram.send", channel="telegram")
    async def send(self, operation: str, payload: Json) -> Json:
        payload = dict(payload)
        routing = payload.pop("_telegram", None)
        raw_target = payload.pop("target")
        if routing and routing["bot_id"] != self.bot_id:
            raise NoEffect("telegram_bot_identity_changed")
        target = int(routing["chat_id"] if routing else raw_target)
        reply_to = payload.pop("reply_to", None)
        kwargs: Json = {"chat_id": target, **payload}
        topic = routing.get("topic_id") if routing else None
        if topic and operation.startswith("send_") or topic and operation == "reply":
            kwargs["message_thread_id"] = topic
        if reply_to is not None:
            kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to, allow_sending_without_reply=True
            )
        fields = {
            "send_photo": "photo",
            "send_document": "document",
            "send_voice": "voice",
            "send_video": "video",
            "send_audio": "audio",
            "send_animation": "animation",
            "send_sticker": "sticker",
            "send_video_note": "video_note",
        }
        artifact_id: str | None = None
        upload: BufferedInputFile | None = None
        cached_file: str | None = None
        if "artifact_id" in kwargs:
            artifact_id = str(kwargs.pop("artifact_id"))
            if operation not in fields:
                raise NoEffect("Unsupported artifact operation")
            metadata, raw = await Artifacts(self.db, self.settings).content(artifact_id)
            upload = BufferedInputFile(raw, filename=metadata["name"])
            cache = await self.db.one(
                "SELECT file_id FROM telegram_file_cache WHERE bot_id=? AND artifact_id=? AND operation=?",
                (self.bot_id, artifact_id, operation),
            )
            cached_file = str(cache["file_id"]) if cache else None
            kwargs[fields[operation]] = cached_file or upload
        if operation == "send_media_group":
            media: list[Any] = []
            types = {
                "photo": InputMediaPhoto,
                "video": InputMediaVideo,
                "audio": InputMediaAudio,
                "document": InputMediaDocument,
            }
            for item in kwargs.pop("items"):
                metadata, raw = await Artifacts(self.db, self.settings).content(item["artifact_id"])
                media.append(
                    types[item["kind"]](
                        media=BufferedInputFile(raw, filename=metadata["name"]),
                        caption=item.get("caption"),
                    )
                )
            kwargs["media"] = media
        mapping = {
            "edit_message": "edit_message_text",
            "reply": "send_message",
            "forward": "forward_message",
            "pin": "pin_chat_message",
            "react": "set_message_reaction",
            "send_buttons": "send_message",
        }
        if operation == "send_poll":
            kwargs["options"] = [InputPollOption(text=text) for text in kwargs["options"]]
        if operation == "send_buttons":
            kwargs["reply_markup"] = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=item["text"], url=item["url"])]
                    for item in kwargs.pop("buttons")
                ]
            )
        if operation == "react":
            kwargs["reaction"] = [ReactionTypeEmoji(emoji=kwargs.pop("emoji"))]
        if operation == "pin":
            kwargs["disable_notification"] = True
        allowed = {
            *fields,
            "send_message",
            "reply",
            "forward",
            "edit_message",
            "delete_message",
            "pin",
            "send_location",
            "send_poll",
            "send_buttons",
            "react",
            "send_media_group",
            "send_contact",
            "send_venue",
        }
        if operation not in allowed:
            raise NoEffect("Unknown Telegram operation")
        try:
            method: Any = getattr(self.bot, mapping.get(operation, operation))
            if routing and operation in ("send_message", "reply") and "reply_markup" not in kwargs:
                from theo.channels.telegram.rendering import rich_html

                rich_kwargs = {k: v for k, v in kwargs.items() if k != "text"}
                try:
                    result = await self.bot.send_rich_message(
                        **rich_kwargs, rich_message=InputRichMessage(html=rich_html(kwargs["text"]))
                    )
                except TelegramBadRequest as exc:
                    if not any(
                        word in exc.message.lower()
                        for word in ("parse", "entity", "rich", "unsupported")
                    ):
                        raise
                    result = await method(**kwargs)
            else:
                try:
                    result = await method(**kwargs)
                except TelegramBadRequest as exc:
                    if not cached_file or not any(
                        word in exc.message.lower()
                        for word in ("file identifier", "file_id", "wrong file", "file reference")
                    ):
                        raise
                    kwargs[fields[operation]] = upload
                    result = await method(**kwargs)
            if artifact_id and not isinstance(result, (bool, list)):
                remote = getattr(result, fields[operation], None)
                if isinstance(remote, list):
                    remote = cast(list[Any], remote)[-1] if remote else None
                if remote and getattr(remote, "file_id", None):
                    # Cache is an optimization: failure cannot turn an accepted send into a retry.
                    with contextlib.suppress(Exception):
                        await self.db.execute(
                            "INSERT OR REPLACE INTO telegram_file_cache VALUES(?,?,?,?)",
                            (self.bot_id, artifact_id, operation, remote.file_id),
                        )
            if isinstance(result, bool):
                if not result:
                    raise NoEffect("telegram_rejected_request")
                return {"accepted": True, "message_id": kwargs.get("message_id")}

            def receipt(message: Any) -> Json:
                item = {
                    "message_id": message.message_id,
                    "chat_id": message.chat.id,
                    "date": message.date.timestamp(),
                }
                if getattr(message, "poll", None):
                    item["poll"] = message.poll.model_dump(mode="json", exclude_none=True)
                return item

            return (
                {
                    "message_id": cast(list[Any], result)[0].message_id,
                    "messages": [receipt(x) for x in cast(list[Any], result)],
                }
                if isinstance(result, list)
                else receipt(result)
            )
        except TelegramRetryAfter as exc:
            raise NoEffect("telegram_rate_limited", float(exc.retry_after)) from None
        except TelegramUnauthorizedError:
            raise NoEffect("telegram_credentials_revoked") from None
        except TelegramForbiddenError:
            raise NoEffect("telegram_bot_blocked_or_permission_missing") from None
        except TelegramBadRequest:
            raise NoEffect("telegram_rejected_request") from None
