"""Receive Telegram updates and coordinate their durable admission.

Owns polling, event recovery and ephemeral previews. Delegates Bot API delivery,
attachment extraction, persisted state and owner controls to dedicated modules.
"""

import asyncio
import contextlib

from aiogram import Bot
from aiogram.exceptions import (
    TelegramRetryAfter,
)
from aiogram.types import (
    BotCommand,
    Update,
)

from theo.channels.telegram.media import TelegramMedia
from theo.channels.telegram.sender import TelegramSender
from theo.channels.telegram.state import TelegramState
from theo.config import Settings
from theo.domain import Denied, Json, encode
from theo.observability import telemetry
from theo.storage import Database
from theo.work.jobs import Jobs


class Telegram:
    def __init__(self, db: Database, settings: Settings, token: str):
        self.db, self.settings, self.owner = db, settings, settings.owner_id
        self.bot = Bot(token=token)
        self.jobs = Jobs(db, self.owner)
        self.state = TelegramState(db, settings, self.bot.id)
        self.username: str | None = None
        self._consumer_lock = asyncio.Lock()
        self._preview_text: dict[str, str] = {}
        self._preview_at: dict[str, float] = {}

    async def setup(self) -> Json:
        me = await self.bot.get_me()
        webhook = await self.bot.get_webhook_info()
        if webhook.url:
            raise Denied("Remove the existing webhook before starting Theo long polling")
        commands = [
            "status",
            "models",
            "backend",
            "jobs",
            "cancel",
            "actions",
            "delivered",
            "schedules",
            "memory",
            "review",
            "goals",
            "usage",
            "pause",
            "resume",
            "help",
        ]
        await self.bot.set_my_commands(
            [BotCommand(command=x, description=x.capitalize()) for x in commands]
        )
        self.username = me.username
        return {"bot_id": me.id, "username": me.username, "polling": True}

    @telemetry.observed("telegram.poll_once", channel="telegram")
    async def poll_once(self) -> int:
        key = f"telegram_offset:{self.state.bot_id}"
        offset = int(
            await self.db.control(self.owner, key)
            or await self.db.control(self.owner, "telegram_offset")
            or "0"
        )
        updates = await self.bot.get_updates(
            offset=offset,
            timeout=25,
            allowed_updates=[
                "message",
                "edited_message",
                "callback_query",
                "message_reaction",
                "message_reaction_count",
                "poll",
                "poll_answer",
                "stopped_message_generation",
                "my_chat_member",
            ],
        )
        for update in updates:
            await self.state.receive(update)
            await self.db.set_control(self.owner, key, str(update.update_id + 1))
        await self.process_pending()
        telemetry.measure("theo_telegram_poll_success_timestamp", self.db.clock(), gauge=True)
        return len(updates)

    @telemetry.observed("telegram.ingest", channel="telegram")
    async def ingest(self, update: Update) -> None:
        await self.state.receive(update)
        await self.process_pending()

    async def process_pending(self) -> None:
        async with self._consumer_lock:
            rows = await self.db.read(
                "SELECT * FROM telegram_events WHERE owner_id=? AND bot_id=? AND status='pending' AND available_at<=? ORDER BY update_id LIMIT 50",
                (self.owner, self.state.bot_id, self.db.clock()),
            )
            for row in rows:
                link = await self.db.one(
                    "SELECT traceparent FROM telemetry_links WHERE kind='telegram' AND entity_id=?",
                    (self.state.event_key(row["update_id"]),),
                )
                with telemetry.operation(
                    "telegram.process",
                    upstream=link["traceparent"] if link else "",
                    channel="telegram",
                ):
                    await self._process_event(row)
            await self.state.flush_albums()

    async def _process_event(self, row: Json) -> None:
        try:
            update = Update.model_validate_json(row["payload"])
            accepted = await self._process(update)
            if not accepted:
                telemetry.mark_outcome("rejected")
            await self.db.execute(
                "INSERT OR IGNORE INTO inbox_updates VALUES(?,?,?,?,?)",
                (
                    self.owner,
                    "telegram",
                    str(update.update_id),
                    row["payload"]
                    if accepted
                    else encode({"rejected": "unsupported_or_not_allowed"}),
                    self.db.clock(),
                ),
            )
            await self.db.execute(
                "UPDATE telegram_events SET status=?,error=NULL WHERE owner_id=? AND bot_id=? AND update_id=?",
                (
                    "done" if accepted else "rejected",
                    self.owner,
                    self.state.bot_id,
                    update.update_id,
                ),
            )
        except Exception as exc:
            attempts = row["attempts"] + 1
            telemetry.mark_outcome("failed" if attempts >= 5 else "retry")
            await self.db.execute(
                "UPDATE telegram_events SET attempts=?,status=?,available_at=?,error=? WHERE owner_id=? AND bot_id=? AND update_id=?",
                (
                    attempts,
                    "failed" if attempts >= 5 else "pending",
                    self.db.clock() + min(60, 2**attempts),
                    type(exc).__name__,
                    self.owner,
                    self.state.bot_id,
                    row["update_id"],
                ),
            )
            await self.db.health(
                self.owner,
                "telegram_normalization_failed",
                {
                    "update_id": row["update_id"],
                    "attempts": attempts,
                    "error": type(exc).__name__,
                },
            )

    async def _process(self, update: Update) -> bool:
        if update.message or update.edited_message:
            return await self.state.message(update, self.username)
        callback = update.callback_query
        if callback:
            if callback.from_user.id != self.settings.telegram_owner_id or not callback.message:
                return False
            conversation = await self.state.destination(
                callback.message.chat.id, getattr(callback.message, "message_thread_id", 0) or 0
            )
            if not conversation:
                return False
            from theo.channels.telegram.controls import TelegramUI

            # The durable operation is independent of acknowledgement succeeding.
            result = await TelegramUI(self.db, self.settings).callback(
                conversation, callback.message.message_id, callback.data or ""
            )
            with contextlib.suppress(Exception):
                await self.bot.answer_callback_query(callback.id, text=result[:200])
            return True
        stopped = update.stopped_message_generation
        if stopped:
            row = await self.db.one(
                "SELECT p.job_id FROM telegram_previews p JOIN jobs j ON j.id=p.job_id WHERE p.chat_id=? AND p.draft_id=? AND j.owner_id=? AND j.generation=p.generation",
                (stopped.chat.id, stopped.draft_id, self.owner),
            )
            if row and stopped.chat.id == self.settings.telegram_chat_id:
                await self.jobs.cancel(row["job_id"])
                return True
            return False
        reaction = update.message_reaction or update.message_reaction_count
        if reaction:
            row = await self.db.one(
                "SELECT * FROM telegram_messages WHERE owner_id=? AND bot_id=? AND chat_id=? AND message_id=?",
                (self.owner, self.state.bot_id, reaction.chat.id, reaction.message_id),
            )
            if not row or (
                update.message_reaction
                and (
                    not update.message_reaction.user
                    or update.message_reaction.user.id != self.settings.telegram_owner_id
                )
            ):
                return False
            kind = "reaction" if update.message_reaction else "reaction_count"
            action = await self.db.one("SELECT run_id FROM actions WHERE id=?", (row["action_id"],))
            await self.db.execute(
                "INSERT OR IGNORE INTO feedback VALUES(?,?,?,?,?,?,?,?)",
                (
                    f"telegram:{self.state.bot_id}:{update.update_id}",
                    self.owner,
                    action["run_id"] if action else None,
                    row["action_id"],
                    kind,
                    encode(
                        {
                            **reaction.model_dump(mode="json", exclude_none=True),
                            "conversation_id": row["conversation_id"],
                        }
                    ),
                    int(kind == "reaction"),
                    self.db.clock(),
                ),
            )
            return True
        poll_id = (
            update.poll.id
            if update.poll
            else update.poll_answer.poll_id
            if update.poll_answer
            else None
        )
        if poll_id:
            poll = await self.db.one(
                "SELECT * FROM telegram_polls WHERE poll_id=? AND owner_id=? AND bot_id=?",
                (poll_id, self.owner, self.state.bot_id),
            )
            if not poll:
                return False
            answer = update.poll_answer
            if answer and (not answer.user or answer.user.id != self.settings.telegram_owner_id):
                return False
            event = answer or update.poll
            assert event
            origin = await self.db.one(
                "SELECT t.action_id,a.run_id FROM telegram_messages t LEFT JOIN actions a ON a.id=t.action_id AND a.owner_id=t.owner_id WHERE t.owner_id=? AND t.bot_id=? AND t.chat_id=? AND t.message_id=?",
                (self.owner, self.state.bot_id, poll["chat_id"], poll["message_id"]),
            )
            await self.db.execute(
                "INSERT OR IGNORE INTO feedback VALUES(?,?,?,?,?,?,?,?)",
                (
                    f"telegram:{self.state.bot_id}:{update.update_id}",
                    self.owner,
                    origin["run_id"] if origin else None,
                    origin["action_id"] if origin else None,
                    "poll_answer" if answer else "poll",
                    encode(
                        {
                            **event.model_dump(mode="json", exclude_none=True),
                            "conversation_id": poll["conversation_id"],
                            "chat_id": poll["chat_id"],
                            "message_id": poll["message_id"],
                        }
                    ),
                    int(bool(answer)),
                    self.db.clock(),
                ),
            )
            return True
        if update.my_chat_member:
            event = update.my_chat_member
            if await self.state.destination(event.chat.id):
                await self.db.health(
                    self.owner,
                    "telegram_membership",
                    {"chat_id": event.chat.id, "status": event.new_chat_member.status},
                )
                return True
        return False

    async def preview(self, job: Json, text: str = "") -> None:
        binding = await self.db.one(
            "SELECT * FROM telegram_destinations WHERE conversation_id=?", (job["conversation_id"],)
        )
        if not binding:
            return
        preview = (self._preview_text.get(job["id"], "") + text)[-4000:]
        # Telegram limits UTF-16 units, so emoji can consume two units each.
        # Drop a cut surrogate pair at the beginning of the rolling preview.
        self._preview_text[job["id"]] = preview.encode("utf-16-le")[-8000:].decode(
            "utf-16-le", errors="ignore"
        )
        has_draft = bool(binding["private"] and self._preview_text[job["id"]])
        timestamp = self.db.clock()
        if timestamp - self._preview_at.get(job["id"], 0) < (1 if has_draft else 4):
            return
        active = await self.db.one("SELECT status,generation FROM jobs WHERE id=?", (job["id"],))
        if not active or active["status"] != "running" or active["generation"] != job["generation"]:
            return
        self._preview_at[job["id"]] = timestamp
        from theo.domain import digest

        draft = int(digest({"job": job["id"], "generation": job["generation"]})[:7], 16) + 1
        await self.db.execute(
            "INSERT INTO telegram_previews VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET generation=excluded.generation,draft_id=excluded.draft_id",
            (job["id"], job["generation"], draft, binding["chat_id"], binding["topic_id"]),
        )
        try:
            if has_draft:
                await self.bot.send_message_draft(
                    chat_id=binding["chat_id"],
                    message_thread_id=binding["topic_id"] or None,
                    draft_id=draft,
                    text=self._preview_text[job["id"]],
                    can_stop=True,
                    keep_on_stop=False,
                    request_timeout=3,
                )
            else:
                await self.bot.send_chat_action(
                    chat_id=binding["chat_id"],
                    message_thread_id=binding["topic_id"] or None,
                    action="typing",
                    request_timeout=3,
                )
        except TelegramRetryAfter as exc:
            self._preview_at[job["id"]] = timestamp + float(exc.retry_after)
        except Exception:
            pass  # Presentation never controls the durable final obligation.

    async def end_preview(self, job_id: str) -> None:
        self._preview_text.pop(job_id, None)
        self._preview_at.pop(job_id, None)
        await self.db.execute("DELETE FROM telegram_previews WHERE job_id=?", (job_id,))

    async def close(self) -> None:
        await self.bot.session.close()

    async def send(self, operation: str, payload: Json) -> Json:
        return await TelegramSender(self.db, self.settings, self.bot, self.state.bot_id).send(
            operation, payload
        )

    async def hydrate(self, part: Json, conversation: str | None = None) -> Json:
        return await TelegramMedia(self.db, self.settings, self.bot).hydrate(part, conversation)
