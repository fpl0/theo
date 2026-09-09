"""Host-owned conversation commands that complete without model inference.

Routes Telegram controls and local slash commands, records their responses
through the delivery ledger and completes jobs without consuming a reasoning slot.
"""

import json
import sqlite3
from collections.abc import Awaitable, Callable

from theo.application.status import status
from theo.channels.telegram.controls import TelegramUI
from theo.config import Settings
from theo.delivery.ledger import Delivery
from theo.domain import (
    Denied,
    Json,
    encode,
)
from theo.storage import Database


class ConversationCommands:
    def __init__(self, db: Database, settings: Settings, cancel: Callable[[str], Awaitable[None]]):
        self.db, self.settings, self.owner = db, settings, settings.owner_id
        self.cancel = cancel

    async def process_pending(self) -> None:
        jobs = await self.db.read(
            "SELECT * FROM jobs WHERE owner_id=? AND status='queued' AND kind='conversation' AND json_extract(payload,'$.text') LIKE '/%' ORDER BY created_at",
            (self.owner,),
        )
        for job in jobs:
            text = json.loads(job["payload"])["text"]
            conv = await self.db.one(
                "SELECT channel FROM conversations WHERE id=?", (job["conversation_id"],)
            )
            if conv and conv["channel"] == "telegram":
                try:
                    incoming = await self.db.one(
                        "SELECT body FROM telegram_messages WHERE owner_id=? AND job_id=? AND action_id IS NULL ORDER BY message_id LIMIT 1",
                        (self.owner, job["id"]),
                    )
                    handled = await TelegramUI(self.db, self.settings).command(
                        job["conversation_id"],
                        text,
                        f"final:{job['id']}",
                        reply=json.loads(incoming["body"]).get("reply") if incoming else None,
                    )
                except (Denied, ValueError) as exc:
                    await TelegramUI(self.db, self.settings).card(
                        job["conversation_id"], str(exc), f"final:{job['id']}"
                    )
                    handled = True
                if handled:
                    await self.db.execute(
                        "UPDATE jobs SET status='completed',outcome=?,updated_at=? WHERE id=? AND status='queued'",
                        (encode({"command": True}), self.db.clock(), job["id"]),
                    )
                    continue
            response = await self.command(job["conversation_id"], text)

            def complete(db: sqlite3.Connection, job: Json = job, response: str = response) -> None:
                Delivery(self.db, self.settings).prepare_in(
                    db,
                    job["conversation_id"],
                    "send_message",
                    {"text": response},
                    f"final:{job['id']}",
                    role="final",
                    durable_obligation=True,
                )
                db.execute(
                    "UPDATE jobs SET status='completed',outcome=?,updated_at=? WHERE id=? AND status='queued'",
                    (encode({"command": True}), self.db.clock(), job["id"]),
                )

            await self.db.write(complete)

    async def command(self, conversation: str, text: str) -> str:
        pieces = text.strip().split()
        command = pieces[0].split("@")[0]
        if command in ("/help", "/start"):
            return "Theo commands: /status /backend [name model] /models /jobs /cancel <job-id> /pause [background|models|notifications] /resume [scope] /memory [query] /goals /usage /help. Requested reminders remain active during background pause."
        if command == "/cancel" and len(pieces) == 2:
            await self.cancel(pieces[1])
            return "Cancellation recorded. Already dispatched effects remain inspectable."
        if command in ("/pause", "/resume"):
            scope = pieces[1] if len(pieces) > 1 else "background"
            if scope not in ("background", "models", "notifications"):
                return "Choose background, models or notifications."
            if command == "/resume" and scope == "background":
                from theo.operations.qualification import qualification_status

                if not (await qualification_status(self.db, self.settings))["production_qualified"]:
                    return "Background activation requires recorded native, Mac, behaviour and seven-day deployment qualification."
            await self.db.set_control(
                self.owner, scope + "_paused", "true" if command == "/pause" else "false"
            )
            return f"{scope.capitalize()} {'paused' if command == '/pause' else 'resumed'}. Requested reminder schedules are preserved."
        if command == "/backend":
            if len(pieces) == 3:
                from theo.backends.policy import BACKENDS

                if pieces[1] not in BACKENDS:
                    return "Available adapters: claude, codex, cursor, grok."
                await self.db.execute(
                    "UPDATE conversations SET backend=?,model=? WHERE id=? AND owner_id=?",
                    (pieces[1], pieces[2], conversation, self.owner),
                )
                return "Route preference saved. Eligibility is verified before each run; canonical memory carries across."
            row = await self.db.one(
                "SELECT backend,model FROM conversations WHERE id=?", (conversation,)
            )
            return encode(row)
        if command == "/models":
            return encode(
                await self.db.read(
                    "SELECT backend,models,status,verified_at FROM backend_accounts WHERE owner_id=?",
                    (self.owner,),
                )
            )
        if command == "/usage":
            from theo.backends.policy import Accounts

            return encode(await Accounts(self.db, self.owner).usage())
        if command == "/jobs":
            return encode(
                await self.db.read(
                    "SELECT id,kind,status,updated_at FROM jobs WHERE owner_id=? ORDER BY created_at DESC LIMIT 20",
                    (self.owner,),
                )
            )
        if command == "/memory":
            from theo.memory.store import Memory

            return encode(await Memory(self.db, self.owner).search(" ".join(pieces[1:]), 10))
        if command == "/goals":
            return encode(
                await self.db.read(
                    "SELECT id,title,status,blocker FROM goals WHERE owner_id=?", (self.owner,)
                )
            )
        if command == "/status":
            return encode(await status(self.db, self.settings))
        return "Unknown command. Use /help."
