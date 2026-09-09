"""Download Telegram attachments and project them into canonical input parts.

Enforces byte limits, labels artifacts with conversation visibility and performs
optional local speech/video extraction while preserving unsupported originals.
"""

import io
from typing import Any

from aiogram import Bot

from theo.config import Settings
from theo.content.artifacts import Artifacts
from theo.domain import Json
from theo.observability import telemetry
from theo.privacy import group_scope, label_in
from theo.storage import Database


class TelegramMedia:
    def __init__(self, db: Database, settings: Settings, bot: Bot):
        self.db, self.settings, self.bot = db, settings, bot
        self.owner = settings.owner_id

    @telemetry.observed("telegram.hydrate", channel="telegram")
    async def hydrate(self, part: Json, conversation: str | None = None) -> Json:
        result = await self._hydrate(part, conversation)
        kind = str(part.get("kind", "unknown"))
        if kind not in {
            "photo",
            "voice",
            "audio",
            "video",
            "animation",
            "sticker",
            "video_note",
            "document",
            "text",
        }:
            kind = "unknown"
        outcome = str(result.get("metadata", {}).get("state", "unchanged"))
        telemetry.measure("theo_telegram_media", kind=kind, outcome=outcome)
        telemetry.event("telegram.media", kind=kind, outcome=outcome, channel="telegram")
        if outcome == "failed":
            telemetry.mark_outcome("failed")
        return result

    async def _hydrate(self, part: Json, conversation: str | None = None) -> Json:
        metadata = part.get("metadata", {})
        if "file_id" not in metadata or part.get("artifact_id"):
            return part
        if metadata.get("size") and metadata["size"] > self.settings.max_media_bytes:
            return {
                **part,
                "text": "Media exceeds configured download limit; original file reference preserved",
                "metadata": {**metadata, "state": "oversized"},
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
                "animation": "animation.mp4",
                "sticker": "sticker.webp",
                "video_note": "video.mp4",
            }.get(part["kind"], "document.bin")
            if part["kind"] == "sticker":
                name = (
                    "sticker.tgs"
                    if metadata.get("animated")
                    else "sticker.webm"
                    if metadata.get("video")
                    else "sticker.webp"
                )
            artifact = await Artifacts(self.db, self.settings).store(
                buffer.getvalue(), name, "Owner Telegram input", preserve_unparsed=True
            )
            scope = await group_scope(self.db, conversation) if conversation else None
            if conversation:
                await self.db.write(
                    lambda connection: label_in(connection, "artifact", artifact["id"], scope)
                )
            row = await self.db.one(
                "SELECT extracted_text FROM artifacts WHERE id=?", (artifact["id"],)
            )
            transcript = None
            derived: list[str] = (
                [artifact["id"]]
                if part["kind"] == "sticker"
                and not metadata.get("animated")
                and not metadata.get("video")
                and artifact["validated"]
                else []
            )
            if part["kind"] in ("video", "video_note", "animation"):
                import tempfile
                from pathlib import Path

                from theo.content.media import video_samples

                try:
                    with tempfile.TemporaryDirectory(prefix="theo-video-") as temporary:
                        movie = Path(temporary) / "input.mp4"
                        movie.write_bytes(buffer.getvalue())
                        for timestamp, frame in await video_samples(movie, Path(temporary)):
                            preview = await Artifacts(self.db, self.settings).store(
                                frame.read_bytes(),
                                f"frame-{timestamp:.2f}.jpg",
                                f"Video sample at {timestamp:.2f}s; partial coverage",
                            )
                            derived.append(preview["id"])
                            if conversation:
                                await self.db.write(
                                    lambda connection, preview=preview: label_in(
                                        connection, "artifact", preview["id"], scope
                                    )
                                )
                except Exception as exc:
                    await self.db.health(
                        self.owner, "video_degraded", {"error": type(exc).__name__}
                    )
            if part["kind"] in ("voice", "audio", "video", "video_note"):
                import tempfile
                from pathlib import Path

                from theo.content.media import transcribe

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
                    "video_coverage": f"{len(derived)} timestamped samples; partial coverage"
                    if derived and part["kind"] in ("video", "video_note", "animation")
                    else None,
                    "state": "failed"
                    if not artifact["validated"]
                    else "ready"
                    if transcript or derived or (row and row["extracted_text"])
                    else "unsupported",
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
                "metadata": {**metadata, "state": "failed"},
            }
