"""Telegram long polling with host-owned durable acknowledgement and rich delivery."""

import io
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputPollOption,
    ReactionTypeEmoji,
    ReplyParameters,
    Update,
)

from theo.artifacts import Artifacts
from theo.config import Settings
from theo.delivery import Delivery, NoEffect
from theo.domain import Denied, Json, encode, uid
from theo.jobs import Jobs
from theo.storage import Database


class Telegram:
    def __init__(self, db: Database, settings: Settings, token: str):
        self.db, self.settings, self.owner = db, settings, settings.owner_id
        self.bot = Bot(token=token)
        self.jobs = Jobs(db, self.owner)

    async def send(self, operation: str, payload: Json) -> Json:
        target = int(payload.pop("target"))
        reply_to = payload.pop("reply_to", None)
        kwargs: Json = {"chat_id": target, **payload}
        if reply_to is not None:
            kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to)
        if "artifact_id" in kwargs:
            artifact_id = kwargs.pop("artifact_id")
            metadata, raw = await Artifacts(self.db, self.settings).content(artifact_id)
            field = {
                "send_photo": "photo",
                "send_document": "document",
                "send_voice": "voice",
                "send_video": "video",
            }[operation]
            kwargs[field] = BufferedInputFile(raw, filename=metadata["name"])
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
            "send_message",
            "reply",
            "forward",
            "edit_message",
            "delete_message",
            "pin",
            "send_photo",
            "send_document",
            "send_voice",
            "send_video",
            "send_location",
            "send_poll",
            "send_buttons",
            "react",
        }
        if operation not in allowed:
            raise NoEffect("Unknown Telegram operation")
        try:
            method: Any = getattr(self.bot, mapping.get(operation, operation))
            result = await method(**kwargs)
            if isinstance(result, bool):
                return {"accepted": result, "message_id": kwargs.get("message_id")}
            return {
                "message_id": result.message_id,
                "chat_id": result.chat.id,
                "date": result.date.timestamp(),
            }
        except TelegramRetryAfter as exc:
            raise NoEffect("telegram_rate_limited", float(exc.retry_after)) from None
        except TelegramBadRequest, TelegramForbiddenError:
            raise NoEffect("telegram_rejected_request") from None

    async def poll_once(self) -> int:
        offset = int(await self.db.control(self.owner, "telegram_offset") or "0")
        updates = await self.bot.get_updates(
            offset=offset,
            timeout=25,
            allowed_updates=[
                "message",
                "edited_message",
                "callback_query",
                "message_reaction",
                "message_reaction_count",
            ],
        )
        for update in updates:
            await self.ingest(update)
            # getUpdates acknowledges older entries only on the NEXT network poll.
            await self.db.set_control(self.owner, "telegram_offset", str(update.update_id + 1))
        return len(updates)

    async def ingest(self, update: Update) -> None:
        payload = update.model_dump(mode="json", exclude_none=True)
        message = update.message or update.edited_message
        callback = update.callback_query
        actor = (
            message.from_user.id
            if message and message.from_user
            else callback.from_user.id
            if callback
            else None
        )
        reaction = update.message_reaction
        if reaction and reaction.user:
            actor = reaction.user.id
        chat_id = (
            message.chat.id
            if message
            else callback.message.chat.id
            if callback and callback.message
            else reaction.chat.id
            if reaction
            else None
        )
        if actor != self.settings.telegram_owner_id or chat_id != self.settings.telegram_chat_id:
            await self.db.execute(
                "INSERT OR IGNORE INTO inbox_updates VALUES(?,?,?,?,?)",
                (
                    self.owner,
                    "telegram",
                    str(update.update_id),
                    encode({"rejected": "owner_or_chat_not_allowed"}),
                    self.db.clock(),
                ),
            )
            return
        conversation = await self.db.conversation(self.owner, "telegram", str(chat_id))
        if callback:
            await self.db.execute(
                "INSERT OR IGNORE INTO inbox_updates VALUES(?,?,?,?,?)",
                (self.owner, "telegram", str(update.update_id), encode(payload), self.db.clock()),
            )
            data = callback.data or ""
            if data.startswith(("approve:", "reject:")):
                try:
                    await Delivery(self.db, self.settings).decide(
                        data.split(":", 1)[1], conversation, data.startswith("approve:")
                    )
                    await callback.answer("Decision recorded")
                except Denied:
                    await callback.answer("This approval is no longer valid")
            return
        if reaction:

            def record(db: Any) -> None:
                changed = db.execute(
                    "INSERT OR IGNORE INTO inbox_updates VALUES(?,?,?,?,?)",
                    (
                        self.owner,
                        "telegram",
                        str(update.update_id),
                        encode(payload),
                        self.db.clock(),
                    ),
                ).rowcount
                if changed:
                    db.execute(
                        "INSERT INTO feedback VALUES(?,?,?,?,?,?,?,?)",
                        (
                            uid(),
                            self.owner,
                            None,
                            None,
                            "reaction",
                            encode(
                                {
                                    "message_id": reaction.message_id,
                                    "new_reaction": [
                                        x.model_dump(mode="json") for x in reaction.new_reaction
                                    ],
                                }
                            ),
                            1,
                            self.db.clock(),
                        ),
                    )
                    db.execute(
                        "UPDATE conversations SET last_engagement=? WHERE id=?",
                        (self.db.clock(), conversation),
                    )

            await self.db.write(record)
            return
        if message:
            text = message.text or message.caption or ""
            parts: list[Json] = []
            media = (
                message.voice
                or message.audio
                or message.document
                or message.video
                or (message.photo[-1] if message.photo else None)
            )
            if media:
                kind = (
                    "voice"
                    if message.voice
                    else "audio"
                    if message.audio
                    else "document"
                    if message.document
                    else "video"
                    if message.video
                    else "photo"
                )
                parts.append(
                    {
                        "kind": kind,
                        "metadata": {
                            "file_id": media.file_id,
                            "size": media.file_size,
                            "name": getattr(media, "file_name", None),
                            "mime": getattr(media, "mime_type", None),
                        },
                    }
                )
            if message.location:
                parts.append(
                    {
                        "kind": "location",
                        "metadata": {
                            "latitude": message.location.latitude,
                            "longitude": message.location.longitude,
                        },
                    }
                )
            if not text and parts:
                text = "[Owner sent " + ", ".join(x["kind"] for x in parts) + "]"
            await self.jobs.ingest(
                conversation, "telegram", str(update.update_id), payload, text, parts
            )

    async def hydrate(self, part: Json) -> Json:
        metadata = part.get("metadata", {})
        if "file_id" not in metadata or part.get("artifact_id"):
            return part
        if metadata.get("size") and metadata["size"] > self.settings.max_media_bytes:
            return {
                **part,
                "text": "Media exceeds configured download limit; original file reference preserved",
            }
        max_media_bytes = self.settings.max_media_bytes

        class BoundedBuffer(io.BytesIO):
            def write(self, data: Any) -> int:
                if self.tell() + len(data) > max_media_bytes:
                    raise ValueError("Telegram download exceeded the media limit")
                return super().write(data)

        buffer = BoundedBuffer()
        try:
            await self.bot.download(metadata["file_id"], destination=buffer)
            name = metadata.get("name") or {
                "photo": "photo.jpg",
                "voice": "voice.ogg",
                "audio": "audio.mp3",
                "video": "video.mp4",
            }.get(part["kind"], "document.bin")
            artifact = await Artifacts(self.db, self.settings).store(
                buffer.getvalue(), name, "Owner Telegram input"
            )
            row = await self.db.one(
                "SELECT extracted_text FROM artifacts WHERE id=?", (artifact["id"],)
            )
            transcript = None
            derived: list[str] = []
            if part["kind"] == "video":
                import tempfile
                from pathlib import Path

                from theo.media import video_keyframe

                try:
                    with tempfile.TemporaryDirectory(prefix="theo-video-") as temporary:
                        movie = Path(temporary) / "input.mp4"
                        movie.write_bytes(buffer.getvalue())
                        frame = await video_keyframe(movie, Path(temporary) / "first-frame.jpg")
                        preview = await Artifacts(self.db, self.settings).store(
                            frame.read_bytes(),
                            "first-frame.jpg",
                            "First video frame only; not full video coverage",
                        )
                        derived.append(preview["id"])
                except Exception as exc:
                    await self.db.health(
                        self.owner, "video_degraded", {"error": type(exc).__name__}
                    )
            if part["kind"] in ("voice", "audio"):
                import tempfile
                from pathlib import Path

                from theo.media import transcribe

                try:
                    with tempfile.TemporaryDirectory(prefix="theo-speech-") as temporary:
                        path = Path(temporary) / Path(name).name
                        path.write_bytes(buffer.getvalue())
                        transcript = await transcribe(path, self.db.root / "models/speech")
                except Exception as exc:
                    await self.db.health(
                        self.owner, "speech_degraded", {"error": type(exc).__name__}
                    )
            return {
                **part,
                "artifact_id": artifact["id"],
                "metadata": {
                    **metadata,
                    "derived_photos": derived,
                    "video_coverage": "first frame only" if derived else None,
                },
                "text": transcript
                or (
                    row["extracted_text"]
                    if row and row["extracted_text"]
                    else "Original media preserved; local extraction or vision is required before describing its contents"
                ),
            }
        except Exception as exc:
            await self.db.health(
                self.owner, "media_degraded", {"kind": part["kind"], "error": type(exc).__name__}
            )
            return {
                **part,
                "text": "Media extraction unavailable; original Telegram reference preserved",
            }

    async def close(self) -> None:
        await self.bot.session.close()
