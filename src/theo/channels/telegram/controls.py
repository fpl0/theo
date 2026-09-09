"""Host-owned Telegram commands, review cards and expiring button capabilities.

Renders owner controls, binds callbacks to exact reviewable actions and applies
approval decisions. Model-generated text cannot create these capabilities.
"""

import json
import secrets
import sqlite3
from datetime import UTC, datetime
from typing import cast
from zoneinfo import ZoneInfo

from theo.config import Settings
from theo.delivery.ledger import Delivery
from theo.domain import Conflict, Denied, Json, TheoError, digest, encode
from theo.memory.store import Memory
from theo.privacy import group_scope
from theo.storage import Database
from theo.work.jobs import Jobs
from theo.work.scheduling import Scheduler


def parse_time(value: str, timezone: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo:
        return moment
    zone = ZoneInfo(timezone)
    first, second = moment.replace(tzinfo=zone, fold=0), moment.replace(tzinfo=zone, fold=1)
    if (
        first.utcoffset() != second.utcoffset()
        or first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != moment
    ):
        raise ValueError("Use an explicit UTC offset for an ambiguous or nonexistent local time")
    return first


def action_preview(operation: str, request: Json, target: str) -> str:
    """Show the authorized content and destination without internal routing IDs."""
    raw_binding = request.get("_telegram")
    destination = target
    if isinstance(raw_binding, dict):
        binding = cast(Json, raw_binding)
        destination = f"Chat {binding['chat_id']}"
        if binding.get("topic_id"):
            destination += f", topic {binding['topic_id']}"
    content = {key: value for key, value in request.items() if key != "_telegram"}
    text = content.pop("text", None)
    caption = content.pop("caption", None)
    lines = [operation.replace("_", " ").capitalize(), "Destination: " + destination]
    if text is not None:
        lines.extend(["", "Message:", str(text)])
    if caption is not None:
        lines.extend(["", "Caption:", str(caption)])
    if content:
        lines.extend(["", "Parameters:", json.dumps(content, ensure_ascii=False, indent=2)])
    return "\n".join(lines)


class TelegramUI:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings, self.owner = db, settings, settings.owner_id

    async def card(
        self,
        conversation: str,
        text: str,
        key: str,
        buttons: list[tuple[str, str, Json]] | None = None,
    ) -> str:
        def queue(db: sqlite3.Connection) -> str:
            existing = db.execute(
                "SELECT id FROM actions WHERE owner_id=? AND semantic_key=?", (self.owner, key)
            ).fetchone()
            if existing:
                return str(existing[0])
            entries = [(secrets.token_urlsafe(18), *button) for button in buttons or []]
            request: Json = {"text": text}
            if entries:
                request["reply_markup"] = {
                    "inline_keyboard": [
                        [{"text": label[:64], "callback_data": "ui:" + token}]
                        for token, label, _, _ in entries
                    ]
                }
            action = Delivery(self.db, self.settings).prepare_in(
                db,
                conversation,
                "send_message",
                request,
                key,
                role="progress",
                durable_obligation=True,
            )
            for token, _, operation, body in entries:
                db.execute(
                    "INSERT INTO telegram_callbacks VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        token,
                        self.owner,
                        conversation,
                        action,
                        operation,
                        encode(body),
                        self.db.clock() + 3600,
                        "pending",
                        None,
                    ),
                )
            return action

        return await self.db.write(queue)

    async def command(
        self, conversation: str, text: str, key: str, *, reply: Json | None = None
    ) -> bool:
        pieces = text.strip().split(maxsplit=2)
        command = pieces[0].split("@")[0]
        argument = pieces[1] if len(pieces) > 1 else ""
        scope = await group_scope(self.db, conversation)
        buttons: list[tuple[str, str, Json]] = []
        if command in ("/start", "/help"):
            output = "Telegram is connected. Send a message, file or voice note. Conversation replies require a verified model route; use /models to check.\n\n/status · /jobs · /schedules · /models · /backend · /memory · /review · /goals · /usage · /pause · /resume\n/cancel JOB_ID stops work. /actions inspects delivery.\n/remind ISO_TIME TEXT creates a reminder. /reschedule ID ISO_TIME changes its next occurrence.\nGroup controls and results stay within their topic; private reviews appear in your private chat."
            buttons = [
                (x.title(), "view", {"command": "/" + x})
                for x in ("status", "jobs", "schedules", "memory", "review")
            ]
        elif command in ("/jobs", "/schedules", "/goals", "/actions", "/memory"):
            page = int(argument) if argument.isdigit() else 0
            page = min(page, 10000)
            table, fields = {
                "/jobs": ("jobs", "id,kind,status,updated_at"),
                "/schedules": ("schedules", "id,body,next_due,active,timezone"),
                "/goals": ("goals", "id,title,status"),
                "/actions": ("actions", "id,operation,status,target"),
                "/memory": ("memory_records", "id,kind,status,revision"),
            }[command]
            if command == "/memory":
                if argument and not argument.isdigit():
                    rows = await Memory(self.db, self.owner, scope).search(" ".join(pieces[1:]), 8)
                else:
                    rows = await self.db.read(
                        "SELECT m.id,r.body,m.status,m.revision FROM memory_records m JOIN memory_revisions r ON r.memory_id=m.id AND r.version=m.revision WHERE m.owner_id=? AND (? IS NULL OR EXISTS(SELECT 1 FROM resource_scopes s WHERE s.kind='memory' AND s.resource_id=m.id AND s.conversation_id=?)) ORDER BY m.updated_at DESC LIMIT 9 OFFSET ?",
                        (self.owner, scope, scope, page * 8),
                    )
            else:
                rows = await self.db.read(
                    f"SELECT {fields} FROM {table} WHERE owner_id=? AND (? IS NULL OR conversation_id=?) ORDER BY rowid DESC LIMIT 9 OFFSET ?",
                    (self.owner, scope, scope, page * 8),
                )
            output = command[1:].title() + f" · page {page + 1}\n"
            for row in rows[:8]:
                details = [
                    str(row[x])[:600]
                    for x in ("body", "title", "kind", "operation", "status", "target")
                    if x in row
                ]
                if "next_due" in row:
                    details.append("active" if row["active"] else "inactive")
                    details.append(
                        datetime.fromtimestamp(
                            row["next_due"], ZoneInfo(row["timezone"])
                        ).isoformat()
                    )
                output += "\n" + " · ".join(details) + "\n" + row["id"] + "\n"
                if table == "jobs" and row["status"] not in ("completed", "failed", "cancelled"):
                    buttons.append(
                        (
                            "Cancel " + row["id"][:8],
                            "cancel",
                            {"id": row["id"], "version": row["updated_at"]},
                        )
                    )
                elif table == "schedules" and row["active"]:
                    buttons.append(
                        (
                            "Cancel reminder " + row["id"][:8],
                            "schedule_cancel",
                            {"id": row["id"], "version": row["next_due"]},
                        )
                    )
                elif table == "memory_records":
                    buttons.append(
                        (
                            "Review " + row["id"][:8],
                            "memory",
                            {"id": row["id"], "revision": row["revision"]},
                        )
                    )
                elif table == "actions":
                    buttons.append(
                        ("Inspect " + row["id"][:8], "inspect_action", {"id": row["id"]})
                    )
                    if row["status"] == "uncertain" and not scope:
                        chunks = await self.db.read(
                            "SELECT id FROM outbox WHERE action_id=? AND status='uncertain' ORDER BY ordinal",
                            (row["id"],),
                        )
                        output += "Confirm delivered only after checking the exact destination and content. Reply to the received message with /delivered ACTION_ID CHUNK_ID, or provide message IDs explicitly.\n"
                        for chunk in chunks:
                            output += f"/delivered {row['id']} {chunk['id']} MESSAGE_IDS\n"
            if not rows:
                output += "\nNothing here yet."
            if page:
                buttons.append(("Previous", "view", {"command": f"{command} {page - 1}"}))
            if len(rows) > 8:
                buttons.append(("Next", "view", {"command": f"{command} {page + 1}"}))
        elif command == "/models":
            rows = await self.db.read(
                "SELECT backend,models,status FROM backend_accounts WHERE owner_id=?", (self.owner,)
            )
            output = "Available model routes\n"
            if not rows:
                output += "\nNo native accounts are configured yet. Complete native account and isolation setup, then choose a verified route here. Commands and reminders remain available."
            for row in rows:
                output += f"\n{row['backend']}: {row['status']}"
                models = json.loads(row["models"])
                for model in models[:8]:
                    if isinstance(model, str):
                        buttons.append(
                            (
                                f"{row['backend']} · {model}",
                                "route",
                                {"backend": row["backend"], "model": model},
                            )
                        )
        elif command == "/usage" and not scope:
            from theo.backends.policy import Accounts

            usage = await Accounts(self.db, self.owner).usage()
            output = "Account usage (unknown values remain unknown)\n" + encode(usage)
        elif command == "/backend" and not argument:
            row = await self.db.one(
                "SELECT backend,model FROM conversations WHERE id=?", (conversation,)
            )
            assert row is not None
            output = f"Current route: {row['backend'] or self.settings.primary_backend or 'not selected'} · {row['model'] or self.settings.primary_model or 'not selected'}\nUse /models to choose a route."
        elif command == "/status":
            jobs = await self.db.read(
                "SELECT status,count(*) n FROM jobs WHERE owner_id=? AND (? IS NULL OR conversation_id=?) GROUP BY status",
                (self.owner, scope, scope),
            )
            output = "Theo status\n" + "\n".join(
                f"{r['status'].replace('_', ' ')}: {r['n']}" for r in jobs
            )
            for name in ("background", "models", "notifications"):
                output += f"\n{name.title()}: {'paused' if await self.db.control(self.owner, name + '_paused') == 'true' else 'enabled'}"
            if not scope:
                events = await self.db.read(
                    "SELECT status,count(*) n FROM telegram_events WHERE owner_id=? AND status IN ('failed','pending') GROUP BY status",
                    (self.owner,),
                )
                output += "\nTelegram inbox: " + (
                    ", ".join(f"{x['n']} {x['status']}" for x in events) or "caught up"
                )
        elif command == "/review":
            if scope:
                output = "Open /review in your private chat for approvals and memory changes."
            else:
                await self.reviews(refresh_key=key)
                output = "Pending review cards are shown in this chat. Use /actions to inspect delivery or uncertainty."
        elif command == "/delivered":
            if scope or not await self.db.one(
                "SELECT 1 FROM telegram_destinations WHERE owner_id=? AND conversation_id=? AND chat_id=? AND private=1",
                (self.owner, conversation, self.settings.telegram_chat_id),
            ):
                raise Denied("Confirm delivery in your private chat")
            fields = text.splitlines()[0].strip().split()
            if len(fields) not in (3, 4):
                raise ValueError(
                    "Reply to the received message with /delivered ACTION_ID CHUNK_ID, or append MESSAGE_IDS after checking the destination. Separate album message IDs with commas."
                )
            row = await self.db.one(
                "SELECT a.request,a.operation,o.status,o.receipt FROM actions a JOIN outbox o ON o.action_id=a.id WHERE a.owner_id=? AND a.id=? AND o.id=?",
                (self.owner, fields[1], fields[2]),
            )
            if not row:
                raise Denied("Delivery unavailable")
            routing = json.loads(row["request"]).get("_telegram")
            if not routing or not (
                row["operation"].startswith("send_") or row["operation"] in ("reply", "forward")
            ):
                raise Denied("Only Telegram delivery receipts can be confirmed here")
            if len(fields) == 3:
                if not reply or (
                    reply.get("chat_id") != routing["chat_id"]
                    or reply.get("topic_id") != routing["topic_id"]
                    or reply.get("sender_id") != routing["bot_id"]
                    or type(reply.get("message_id")) is not int
                ):
                    raise Denied(
                        "Reply to a message from this bot in the original destination, or provide the checked message IDs explicitly"
                    )
                message_ids = [int(reply["message_id"])]
            else:
                try:
                    message_ids = [int(value) for value in fields[3].split(",")]
                except ValueError:
                    raise ValueError(
                        "Received message IDs must be positive integers separated by commas"
                    ) from None
            if (
                not message_ids
                or any(mid <= 0 for mid in message_ids)
                or len(set(message_ids)) != len(message_ids)
            ):
                raise ValueError("Received message IDs must be positive and unique")
            receipt = {
                "message_id": message_ids[0],
                "messages": [
                    {
                        "message_id": mid,
                        "chat_id": routing["chat_id"],
                        "topic_id": routing["topic_id"],
                    }
                    for mid in message_ids
                ],
                "operator_confirmed": True,
            }
            if row["status"] == "succeeded" and row["receipt"] == encode(receipt):
                output = "This delivery confirmation was already recorded. Nothing was resent."
            else:
                try:
                    await Delivery(self.db, self.settings).reconcile(
                        fields[1], receipt=receipt, delivery_id=fields[2]
                    )
                except Conflict as exc:
                    raise Denied(str(exc)) from None
                output = "Your delivery confirmation is recorded. Nothing was resent."
        elif command == "/cancel" and argument:
            if scope and not await self.db.one(
                "SELECT 1 FROM jobs WHERE id=? AND owner_id=? AND conversation_id=?",
                (argument, self.owner, scope),
            ):
                output = "That job is not available in this topic."
            else:
                await Jobs(self.db, self.owner).cancel(argument)
                output = "Cancellation recorded. Effects already dispatched remain inspectable."
        elif command in ("/remind", "/reschedule"):
            try:
                if command == "/remind":
                    when = parse_time(argument, self.settings.timezone)
                    schedule = await Scheduler(self.db, self.owner).create(
                        conversation,
                        pieces[2],
                        due=when.timestamp(),
                        timezone=self.settings.timezone,
                        idempotency_key=key,
                    )
                else:
                    when = parse_time(pieces[2], self.settings.timezone)
                    await Scheduler(self.db, self.owner).reschedule(
                        argument, when.timestamp(), scope
                    )
                    schedule = argument
                output = f"Reminder saved for {when.isoformat()}.\n{schedule}"
            except ValueError, IndexError:
                output = "Use /remind ISO_TIME TEXT or /reschedule ID ISO_TIME. Include a UTC offset for an ambiguous local time."
        elif scope and command in ("/usage", "/pause", "/resume"):
            output = "Use your private chat for account usage and global controls."
        else:
            return False
        await self.card(conversation, output, key, buttons)
        return True

    async def reviews(self, *, refresh_key: str | None = None) -> None:
        revision = refresh_key or "initial"
        private = await self.db.one(
            "SELECT conversation_id FROM telegram_destinations WHERE owner_id=? AND chat_id=? AND private=1 ORDER BY topic_id LIMIT 1",
            (self.owner, self.settings.telegram_chat_id),
        )
        if not private:
            return
        conversation = private["conversation_id"]
        rows = await self.db.read(
            "SELECT p.*,a.operation,a.request FROM approvals p JOIN actions a ON a.id=p.action_id WHERE p.owner_id=? AND p.decision='pending' AND a.status='awaiting_approval' AND p.expires_at>? LIMIT 10",
            (self.owner, self.db.clock()),
        )
        for row in rows:
            request = json.loads(row["request"])
            artifact_ids = [request["artifact_id"]] if request.get("artifact_id") else []
            artifact_ids.extend(
                item["artifact_id"] for item in request.get("items", []) if item.get("artifact_id")
            )
            previews: list[str] = []
            for artifact_id in dict.fromkeys(artifact_ids):
                previews.append(
                    await Delivery(self.db, self.settings).prepare(
                        conversation,
                        "send_document",
                        {
                            "artifact_id": artifact_id,
                            "caption": f"Review attachment for {row['operation']} → {row['target']}",
                        },
                        f"approval-preview:{row['id']}:{artifact_id}",
                        role="progress",
                        durable_obligation=True,
                    )
                )
            body = {"id": row["id"], "hash": row["request_hash"], "previews": previews}
            await self.card(
                conversation,
                "Approval required\n"
                + action_preview(row["operation"], request, row["target"])
                + "\n\nExpires: "
                + datetime.fromtimestamp(
                    row["expires_at"], ZoneInfo(self.settings.timezone)
                ).isoformat(timespec="seconds"),
                f"review:{row['id']}:{revision}",
                [
                    ("Approve", "approve", body),
                    ("Reject", "reject", body),
                    (
                        "Details",
                        "inspect_action",
                        {"id": row["action_id"], "hash": row["request_hash"]},
                    ),
                ],
            )
        corrections = await self.db.read(
            "SELECT * FROM corrections WHERE owner_id=? AND status='proposed' LIMIT 10",
            (self.owner,),
        )
        for row in corrections:
            body = {"id": row["id"], "revision": row["expected_revision"]}
            await self.card(
                conversation,
                "Memory correction\n" + row["body"],
                f"correction:{row['id']}:{revision}",
                [
                    ("Accept correction", "correction_accept", body),
                    ("Reject", "correction_reject", body),
                ],
            )

        facts = await self.db.read(
            "SELECT * FROM proposals WHERE owner_id=? AND kind='fact' AND status='proposed' LIMIT 10",
            (self.owner,),
        )
        for row in facts:
            body = {"id": row["id"], "hash": digest(row["body"])}
            await self.card(
                conversation,
                "Fact proposal\n" + row["body"],
                f"fact:{row['id']}:{revision}",
                [("Accept fact", "fact_accept", body), ("Reject", "fact_reject", body)],
            )

    async def callback(self, conversation: str, message_id: int, data: str) -> str:
        if not data.startswith("ui:"):
            return "This control is unavailable. Open /help."
        token = data[3:]

        def claim(db: sqlite3.Connection) -> Json:
            row = db.execute(
                "SELECT * FROM telegram_callbacks WHERE token=? AND owner_id=? AND conversation_id=?",
                (token, self.owner, conversation),
            ).fetchone()
            if not row or row["expires_at"] <= self.db.clock():
                raise Denied("This control expired. Open a fresh view.")
            if not db.execute(
                "SELECT 1 FROM telegram_messages WHERE conversation_id=? AND message_id=? AND action_id=?",
                (conversation, message_id, row["action_id"]),
            ).fetchone():
                raise Denied("This control belongs to another message")
            if row["status"] != "pending":
                return {**dict(row), "already": True}
            db.execute("UPDATE telegram_callbacks SET status='executing' WHERE token=?", (token,))
            return dict(row)

        try:
            row = await self.db.write(claim)
            if row.get("already"):
                return (
                    row["result"]
                    or "This operation was already started. Inspect its state before retrying."
                )
            operation, body = row["operation"], json.loads(row["body"])
            result = await self._act(conversation, operation, body, token)
            await self.db.execute(
                "UPDATE telegram_callbacks SET status='done',result=? WHERE token=?",
                (result, token),
            )
            return result
        except TheoError as exc:
            return str(exc)

    async def _act(self, conversation: str, operation: str, body: Json, token: str) -> str:
        scope = await group_scope(self.db, conversation)
        if operation == "view":
            await self.command(conversation, body["command"], "view:" + token)
        elif operation in ("approve", "reject"):
            if scope:
                raise Denied("Review this action in your private chat")
            row = await self.db.one(
                "SELECT a.conversation_id,p.request_hash FROM approvals p JOIN actions a ON a.id=p.action_id WHERE p.id=? AND p.owner_id=?",
                (body["id"], self.owner),
            )
            if not row or row["request_hash"] != body["hash"]:
                raise Denied("Action changed; review it again")
            if operation == "approve":
                for preview in body.get("previews", []):
                    delivery = await self.db.one(
                        "SELECT status FROM actions WHERE id=? AND owner_id=?",
                        (preview, self.owner),
                    )
                    if not delivery or delivery["status"] != "succeeded":
                        raise Denied(
                            "The attachment preview is not delivered yet. Inspect delivery, then open /review again."
                        )
            await Delivery(self.db, self.settings).decide(
                body["id"], row["conversation_id"], operation == "approve"
            )
        elif operation in ("correction_accept", "correction_reject"):
            if scope:
                raise Denied("Review corrections privately")
            await Memory(self.db, self.owner).review(body["id"], operation == "correction_accept")
        elif operation in ("fact_accept", "fact_reject"):
            if scope:
                raise Denied("Review facts privately")
            row = await self.db.one(
                "SELECT * FROM proposals WHERE id=? AND owner_id=? AND kind='fact' AND status='proposed'",
                (body["id"], self.owner),
            )
            if not row or digest(row["body"]) != body["hash"]:
                raise Denied("Fact proposal changed or already decided")
            if operation == "fact_accept":
                proposal = json.loads(row["body"])
                source = await self.db.one(
                    "SELECT conversation_id FROM messages WHERE id=? AND owner_id=?",
                    (proposal["source_message_id"], self.owner),
                )
                if not source:
                    raise Denied("Source unavailable")
                source_scope = await group_scope(self.db, source["conversation_id"])
                if source_scope:
                    # Facts have an owner-wide semantic key. Do not relabel an existing private fact.
                    existing = await self.db.one(
                        "SELECT id FROM facts WHERE owner_id=? AND subject=? AND predicate=?",
                        (self.owner, proposal["subject"], proposal["predicate"]),
                    )
                    if existing:
                        from theo.privacy import require_resource

                        await require_resource(self.db, "fact", existing["id"], source_scope)
                fact = await Memory(self.db, self.owner).set_fact(
                    proposal["subject"],
                    proposal["predicate"],
                    proposal["value"],
                    "review:" + body["id"],
                    expected=proposal.get("expected_revision", 0),
                    valid_to=proposal.get("valid_to"),
                )
                if source_scope:
                    await self.db.execute(
                        "INSERT OR IGNORE INTO resource_scopes VALUES('fact',?,?)",
                        (fact, source_scope),
                    )
            await self.db.execute(
                "UPDATE proposals SET status=? WHERE id=? AND status='proposed'",
                ("active" if operation == "fact_accept" else "rejected", body["id"]),
            )
        elif operation == "route":
            await self.db.execute(
                "UPDATE conversations SET backend=?,model=? WHERE id=? AND owner_id=?",
                (body["backend"], body["model"], conversation, self.owner),
            )
        elif operation == "cancel":
            row = await self.db.one(
                "SELECT updated_at FROM jobs WHERE id=? AND owner_id=? AND (? IS NULL OR conversation_id=?)",
                (body["id"], self.owner, scope, scope),
            )
            if not row or row["updated_at"] != body["version"]:
                raise Denied("Job changed. Open /jobs for current controls.")
            await Jobs(self.db, self.owner).cancel(body["id"])
        elif operation == "schedule_cancel":
            row = await self.db.one(
                "SELECT next_due FROM schedules WHERE id=? AND owner_id=? AND (? IS NULL OR conversation_id=?)",
                (body["id"], self.owner, scope, scope),
            )
            if not row or row["next_due"] != body["version"]:
                raise Denied("Reminder changed. Open /schedules again.")
            await Scheduler(self.db, self.owner).cancel(body["id"])
        elif operation in ("memory", "archive", "restore"):
            memory = Memory(self.db, self.owner, scope)
            row = await memory.show(body["id"])
            if row["revision"] != body["revision"]:
                raise Denied("Memory changed. Open /memory again.")
            if operation == "memory":
                history = await memory.history(body["id"])
                await self.card(
                    conversation,
                    "Memory history\n"
                    + "\n\n".join(f"Revision {x['version']}: {x['body']}" for x in history[-5:]),
                    "memory:" + token,
                    [
                        (
                            "Archive" if row["status"] == "active" else "Restore",
                            "archive" if row["status"] == "active" else "restore",
                            body,
                        )
                    ],
                )
            elif operation == "archive":
                await memory.archive(body["id"])
            else:
                await memory.restore(body["id"])
        elif operation == "inspect_action":
            row = await self.db.one(
                "SELECT * FROM actions WHERE id=? AND owner_id=? AND (? IS NULL OR conversation_id=?)",
                (body["id"], self.owner, scope, scope),
            )
            if not row:
                raise Denied("Action unavailable")
            if body.get("hash") and row["request_hash"] != body["hash"]:
                raise Denied("Action changed; open a fresh review")
            chunks = await self.db.read(
                "SELECT id,status,error,receipt FROM outbox WHERE action_id=? ORDER BY ordinal",
                (body["id"],),
            )
            buttons = (
                [
                    (
                        "Confirm no effect: " + x["id"][:8],
                        "no_effect",
                        {"id": body["id"], "chunk": x["id"], "hash": row["request_hash"]},
                    )
                    for x in chunks
                    if x["status"] == "uncertain"
                ]
                if not scope
                else []
            )
            await self.card(
                conversation,
                f"{row['operation']} → {row['target']}\n{row['status']}\n{row['request']}\n\nReceipts: {encode(chunks)}\nOnly confirm no effect after checking the destination yourself. If the message arrived, use /delivered ACTION_ID CHUNK_ID MESSAGE_IDS; this records your confirmation without resending.",
                "inspect:" + token,
                buttons,
            )
        elif operation == "no_effect":
            if scope:
                raise Denied("Reconcile privately")
            row = await self.db.one(
                "SELECT request_hash FROM actions WHERE id=? AND owner_id=?",
                (body["id"], self.owner),
            )
            if not row or row["request_hash"] != body["hash"]:
                raise Denied("Action changed")
            await Delivery(self.db, self.settings).reconcile(
                body["id"], confirmed_no_effect=True, delivery_id=body["chunk"]
            )
        else:
            raise Denied("Control unavailable")
        return "Decision recorded."
