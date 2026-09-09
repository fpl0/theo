"""Read Telegram transport health, backlog and unresolved delivery state.

Combines persisted queue state with optional read-only Bot API checks when a
token is provided. Reports errors without exposing credential-bearing URLs.
"""

import shutil

from aiogram import Bot

from theo.config import Settings
from theo.domain import Json
from theo.storage import Database


async def diagnostics(db: Database, settings: Settings, token: str | None = None) -> Json:
    report: Json = {
        "configured": bool(settings.telegram_owner_id and settings.telegram_chat_id),
        "credentials_available": bool(token),
        "speech_assets": (db.root / "models/speech").exists(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
    }
    report["inbox"] = await db.read(
        "SELECT status,count(*) count,min(received_at) oldest_at FROM telegram_events WHERE owner_id=? GROUP BY status",
        (settings.owner_id,),
    )
    report["outbox"] = await db.read(
        "SELECT o.status,count(*) count,min(o.available_at) oldest_at FROM outbox o JOIN actions a ON a.id=o.action_id JOIN conversations c ON c.id=a.conversation_id WHERE o.owner_id=? AND c.channel='telegram' GROUP BY o.status",
        (settings.owner_id,),
    )
    report["destinations"] = await db.read(
        "SELECT id,chat_id,topic_id,private FROM telegram_destinations WHERE owner_id=?",
        (settings.owner_id,),
    )
    report["media_failures"] = await db.read(
        "SELECT kind,count(*) count FROM health_events WHERE owner_id=? AND kind IN ('media_degraded','speech_degraded','video_degraded') GROUP BY kind",
        (settings.owner_id,),
    )
    if token:
        bot = Bot(token=token)
        try:
            me = await bot.get_me(request_timeout=10)
            webhook = await bot.get_webhook_info(request_timeout=10)
            report.update(bot_id=me.id, username=me.username, webhook_conflict=bool(webhook.url))
            permissions: list[Json] = []
            for chat in {
                settings.telegram_chat_id,
                *(d.chat_id for d in settings.telegram_destinations),
            } - {None}:
                if chat is None:
                    continue
                try:
                    member = await bot.get_chat_member(
                        chat_id=int(chat), user_id=me.id, request_timeout=10
                    )
                    permissions.append(
                        {
                            "chat_id": chat,
                            "status": member.status,
                            "permissions": member.model_dump(mode="json", exclude={"user"}),
                        }
                    )
                except Exception as exc:
                    permissions.append({"chat_id": chat, "error": type(exc).__name__})
            report["permissions"] = permissions
        except Exception as exc:
            report["transport_error"] = type(exc).__name__
        finally:
            await bot.session.close()
    return report
