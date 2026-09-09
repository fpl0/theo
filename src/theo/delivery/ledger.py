"""Transactional action intents, approvals and multipart delivery receipts.

Owns delivery policy, outbox claiming and exact-chunk reconciliation. Network
calls occur outside transactions; uncertain effects require evidence before retry.
"""

import json
import sqlite3
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from theo.config import Settings
from theo.delivery.chunking import split_text
from theo.delivery.contracts import NoEffect, Sender
from theo.domain import Conflict, Denied, Json, digest, encode, uid
from theo.observability import telemetry
from theo.storage import Database
from theo.work.jobs import Jobs


class Delivery:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings, self.owner = db, settings, settings.owner_id

    def prepare_in(
        self,
        db: sqlite3.Connection,
        conversation: str,
        operation: str,
        request: Json,
        key: str,
        *,
        job_id: str | None = None,
        run_id: str | None = None,
        generation: int | None = None,
        role: str = "final",
        autonomous: bool = False,
        discretionary: bool = False,
        is_ask: bool = False,
        freshness: Json | None = None,
        expires_at: float | None = None,
        target: str | None = None,
        require_approval: bool = False,
        durable_obligation: bool = False,
        destination_id: str | None = None,
    ) -> str:
        conv = db.execute(
            "SELECT * FROM conversations WHERE id=? AND owner_id=?", (conversation, self.owner)
        ).fetchone()
        if conv is None:
            raise Denied("Conversation unavailable")
        if job_id and generation is not None:
            Jobs(self.db, self.owner).check(db, job_id, generation)
        request = dict(request)
        binding = None
        if destination_id:
            binding = db.execute(
                "SELECT d.*,c.target FROM telegram_destinations d JOIN conversations c ON c.id=d.conversation_id WHERE d.id=? AND d.owner_id=?",
                (destination_id, self.owner),
            ).fetchone()
            if binding is None:
                raise Denied("Telegram destination unavailable")
            target = str(binding["target"])
        elif conv["channel"] == "telegram":
            binding = db.execute(
                "SELECT d.*,c.target FROM telegram_destinations d JOIN conversations c ON c.id=d.conversation_id WHERE d.owner_id=? AND c.target=?",
                (self.owner, target or conv["target"]),
            ).fetchone()
            if binding is None and target and target != conv["target"]:
                raise Denied("Register this Telegram destination before sending")
        if binding:
            request["_telegram"] = {
                "bot_id": binding["bot_id"],
                "chat_id": binding["chat_id"],
                "topic_id": binding["topic_id"],
                "conversation_id": binding["conversation_id"],
            }
            if (
                job_id
                and binding["conversation_id"] == conversation
                and operation
                in (
                    "send_message",
                    "reply",
                    "send_photo",
                    "send_document",
                    "send_voice",
                    "send_video",
                    "send_audio",
                    "send_animation",
                    "send_media_group",
                )
            ):
                input_job = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
                reply = json.loads(input_job[0]).get("reply_to") if input_job else None
                if reply and "reply_to" not in request:
                    request["reply_to"] = reply
        recipient = target or str(conv["target"])
        external = (
            recipient != str(conv["target"])
            or require_approval
            or bool(destination_id and conv["channel"] != "telegram")
        )
        canonical = {"operation": operation, "request": request, "target": recipient, "role": role}
        request_hash = digest(canonical)
        existing = db.execute(
            "SELECT id,request_hash FROM actions WHERE owner_id=? AND semantic_key=?",
            (self.owner, key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise Conflict("Logical action already binds different content")
            return str(existing["id"])
        action_id, timestamp = uid(), self.db.clock()
        scope = "obligation" if durable_obligation else "draft"
        status = "awaiting_approval" if external else "ready"
        expiry = expires_at if expires_at is not None else timestamp + 7 * 86400
        db.execute(
            "INSERT INTO actions(id,owner_id,conversation_id,job_id,run_id,generation,semantic_key,operation,request,request_hash,target,scope,policy_version,status,expires_at,source_sequence,freshness,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                action_id,
                self.owner,
                conversation,
                job_id,
                run_id,
                generation,
                key,
                operation,
                encode(request),
                request_hash,
                recipient,
                scope,
                1,
                status,
                expiry,
                conv["sequence"],
                encode(freshness or {}),
                timestamp,
                timestamp,
            ),
        )
        chunks = [request]
        if operation in ("send_message", "reply"):
            chunks = [{**request, "text": text} for text in split_text(str(request["text"]))]
            if "reply_markup" in request:
                chunks = [
                    {k: v for k, v in chunk.items() if k != "reply_markup"}
                    if index < len(chunks) - 1
                    else chunk
                    for index, chunk in enumerate(chunks)
                ]
        elif "caption" in request and (
            operation in ("send_sticker", "send_video_note")
            or len(str(request["caption"]).encode("utf-16-le")) // 2 > 1024
        ):
            caption = str(request["caption"])
            chunks = [
                {k: v for k, v in request.items() if k != "caption"}
                if operation in ("send_sticker", "send_video_note")
                else {**request, "caption": ""},
                *[
                    {
                        "text": x,
                        "_operation": "send_message",
                        **({"reply_to": request["reply_to"]} if "reply_to" in request else {}),
                    }
                    for x in split_text(caption)
                ],
            ]
        for ordinal, chunk in enumerate(chunks):
            db.execute(
                "INSERT INTO outbox(id,owner_id,action_id,ordinal,payload,status,role,autonomous,discretionary,is_ask,available_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    uid(),
                    self.owner,
                    action_id,
                    ordinal,
                    encode(chunk),
                    "ready",
                    role,
                    int(autonomous),
                    int(discretionary),
                    int(is_ask),
                    timestamp,
                ),
            )
        if external:
            db.execute(
                "INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    uid(),
                    self.owner,
                    action_id,
                    request_hash,
                    recipient,
                    scope,
                    expiry,
                    "pending",
                    None,
                ),
            )
        return action_id

    async def prepare(
        self, conversation: str, operation: str, request: Json, key: str, **kwargs: Any
    ) -> str:
        return await self.db.write(
            lambda db: self.prepare_in(db, conversation, operation, request, key, **kwargs)
        )

    async def decide(self, approval_id: str, conversation: str, approve: bool) -> str:
        def decide(db: sqlite3.Connection) -> str:
            row = db.execute(
                "SELECT p.*,a.conversation_id,a.request_hash current_hash,a.status action_status FROM approvals p JOIN actions a ON a.id=p.action_id WHERE p.id=? AND p.owner_id=?",
                (approval_id, self.owner),
            ).fetchone()
            if (
                row is None
                or row["conversation_id"] != conversation
                or row["expires_at"] <= self.db.clock()
                or row["request_hash"] != row["current_hash"]
                or row["decision"] != "pending"
                or row["action_status"] != "awaiting_approval"
            ):
                raise Denied("Approval is stale, unavailable or not bound to this chat")
            db.execute(
                "UPDATE approvals SET decision=?,decided_at=? WHERE id=?",
                ("approved" if approve else "rejected", self.db.clock(), approval_id),
            )
            db.execute(
                "UPDATE actions SET status=? WHERE id=?",
                ("ready" if approve else "cancelled", row["action_id"]),
            )
            if not approve:
                db.execute(
                    "UPDATE outbox SET status='cancelled' WHERE action_id=? AND status='ready'",
                    (row["action_id"],),
                )
            return str(row["action_id"])

        return await self.db.write(decide)

    def _policy(self, db: sqlite3.Connection, row: sqlite3.Row) -> str | None:
        settings = self.settings
        conv = db.execute(
            "SELECT * FROM conversations WHERE id=?", (row["conversation_id"],)
        ).fetchone()
        assert conv
        if self._job_cancelled(db, row["action_id"]):
            return "job_cancelled"
        if row["target"] != conv["target"]:
            approval = db.execute(
                "SELECT 1 FROM approvals WHERE action_id=? AND owner_id=? AND request_hash=? AND target=? AND expires_at>? AND decision='approved'",
                (row["action_id"], self.owner, row["request_hash"], row["target"], self.db.clock()),
            ).fetchone()
            if not approval:
                return "authorization_missing"
        if row["expires_at"] <= self.db.clock():
            return "expired"
        if row["scope"] == "draft":
            newer = db.execute(
                "SELECT 1 FROM messages WHERE conversation_id=? AND role='user' AND sequence>?",
                (row["conversation_id"], row["source_sequence"]),
            ).fetchone()
            if newer:
                return "new_owner_input"
        for fact_id, revision in json.loads(row["freshness"]).items():
            fact = db.execute(
                "SELECT revision FROM facts WHERE id=? AND owner_id=? AND status='active'",
                (fact_id, self.owner),
            ).fetchone()
            if fact is None or fact[0] != revision:
                return "stale_fact"
        if row["discretionary"]:
            if row["critic_status"] != "passed":
                return "critic_" + row["critic_status"]
            local = datetime.fromtimestamp(self.db.clock(), ZoneInfo(settings.timezone))
            hour = local.hour
            quiet = (
                (hour >= settings.quiet_start or hour < settings.quiet_end)
                if settings.quiet_start > settings.quiet_end
                else settings.quiet_start <= hour < settings.quiet_end
            )
            if quiet:
                return "quiet_hours"
            if row["is_ask"]:
                asks = db.execute(
                    "SELECT count(DISTINCT o.action_id),min(r.delivered_at) FROM outbox o JOIN delivery_receipts r ON r.delivery_id=o.id JOIN actions a ON a.id=o.action_id WHERE o.owner_id=? AND a.conversation_id=? AND o.is_ask=1 AND r.delivered_at>?",
                    (self.owner, row["conversation_id"], conv["last_engagement"] or 0),
                ).fetchone()
                if asks and asks[0] >= 3 and self.db.clock() - asks[1] >= 86400:
                    return "unanswered_asks"
        if row["autonomous"]:
            local = datetime.fromtimestamp(self.db.clock(), ZoneInfo(settings.timezone))
            day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            counts = db.execute(
                "SELECT count(DISTINCT CASE WHEN r.delivered_at>? THEN o.action_id END),count(DISTINCT CASE WHEN r.delivered_at>? THEN o.action_id END) FROM outbox o JOIN delivery_receipts r ON r.delivery_id=o.id WHERE o.owner_id=? AND o.autonomous=1 AND o.action_id<>?",
                (self.db.clock() - 3600, day_start, self.owner, row["action_id"]),
            ).fetchone()
            if counts and (
                counts[0] >= settings.autonomous_hour_cap
                or counts[1] >= settings.autonomous_day_cap
            ):
                return "attention_cap"
        return None

    async def dispatch_one(self, send: Sender) -> bool:
        def claim(db: sqlite3.Connection) -> Json | None:
            controls = {
                r[0]: r[1]
                for r in db.execute("SELECT key,value FROM control WHERE owner_id=?", (self.owner,))
            }
            if (
                controls.get("quarantined") == "true"
                or controls.get("notifications_paused") == "true"
            ):
                return None
            row = db.execute(
                "SELECT o.*,a.operation,a.target,a.request_hash,a.conversation_id,a.scope,a.expires_at,a.source_sequence,a.freshness,a.request,a.run_id,a.critic_status FROM outbox o JOIN actions a ON a.id=o.action_id WHERE o.owner_id=? AND o.status='ready' AND a.status='ready' AND o.available_at<=? AND NOT EXISTS(SELECT 1 FROM outbox p WHERE p.action_id=o.action_id AND p.ordinal<o.ordinal AND p.status<>'succeeded') ORDER BY o.available_at,o.ordinal LIMIT 1",
                (self.owner, self.db.clock()),
            ).fetchone()
            if row is None:
                return None
            reason = self._policy(db, row)
            if reason:
                if reason in (
                    "expired",
                    "new_owner_input",
                    "stale_fact",
                    "authorization_missing",
                    "job_cancelled",
                ):
                    db.execute(
                        "UPDATE actions SET status='cancelled',error=? WHERE id=?",
                        (reason, row["action_id"]),
                    )
                    db.execute(
                        "UPDATE outbox SET status='cancelled',error=? WHERE action_id=? AND status='ready'",
                        (reason, row["action_id"]),
                    )
                else:
                    db.execute(
                        "UPDATE outbox SET available_at=?,error=? WHERE id=?",
                        (self.db.clock() + 300, reason, row["id"]),
                    )
                return None
            db.execute(
                "UPDATE outbox SET status='executing',attempts=attempts+1 WHERE id=?", (row["id"],)
            )
            db.execute(
                "UPDATE actions SET status='executing',updated_at=? WHERE id=?",
                (self.db.clock(), row["action_id"]),
            )
            return dict(row)

        row = await self.db.write(claim)
        if row is None:
            return False
        payload = json.loads(row["payload"])
        operation = payload.pop("_operation", row["operation"])
        routing = json.loads(row["request"]).get("_telegram")
        if routing:
            payload["_telegram"] = routing
        channel = await self.db.one(
            "SELECT channel FROM conversations WHERE id=? AND owner_id=?",
            (row["conversation_id"], self.owner),
        )
        link = await self.db.one(
            "SELECT traceparent FROM telemetry_links WHERE kind='run' AND entity_id=?",
            (row["run_id"],),
        )
        with telemetry.operation(
            "delivery.send",
            upstream=link["traceparent"] if link else "",
            channel="telegram"
            if routing or (channel and channel["channel"] == "telegram")
            else "cli",
        ):
            return await self._send_claimed(send, row, payload, operation, routing, channel)

    async def _send_claimed(
        self,
        send: Sender,
        row: Json,
        payload: Json,
        operation: str,
        routing: Json | None,
        channel: Json | None,
    ) -> bool:
        telemetry_channel = (
            "telegram" if routing or (channel and channel["channel"] == "telegram") else "cli"
        )
        try:
            receipt = await send(
                operation,
                {
                    **payload,
                    "target": row["target"],
                    "_channel": "telegram"
                    if routing
                    else channel["channel"]
                    if channel
                    else "local",
                },
            )
        except NoEffect as exc:
            retry = exc.retry_after is not None and row["attempts"] < 6
            retry_after, error_text = exc.retry_after, str(exc)

            def rejected(db: sqlite3.Connection) -> None:
                cancelled = self._job_cancelled(db, row["action_id"])
                db.execute(
                    "UPDATE outbox SET status=?,available_at=?,error=? WHERE id=?",
                    (
                        "cancelled" if cancelled else "ready" if retry else "failed",
                        self.db.clock() + max(retry_after or 0, 1),
                        "job_cancelled" if cancelled else error_text,
                        row["id"],
                    ),
                )
                self._settle_action(db, row["action_id"])

            telemetry.mark_outcome("retry" if retry else "failed")
            telemetry.measure(
                "theo_deliveries", outcome="retry" if retry else "failed", channel=telemetry_channel
            )
            telemetry.event("delivery.rejected", outcome="retry" if retry else "failed")
            await self.db.write(rejected)
            return True
        except BaseException as exc:
            error_name = type(exc).__name__
            telemetry.mark_outcome("uncertain")
            telemetry.measure("theo_deliveries", outcome="uncertain", channel=telemetry_channel)
            telemetry.event("delivery.uncertain", error_type=error_name)
            await self.db.write(
                lambda db: (
                    db.execute(
                        "UPDATE outbox SET status='uncertain',error=? WHERE id=?",
                        (error_name, row["id"]),
                    ),
                    db.execute(
                        "UPDATE actions SET status='uncertain',error='remote_acceptance_unknown' WHERE id=?",
                        (row["action_id"],),
                    ),
                )
            )
            if not isinstance(exc, Exception):
                raise
            return True

        await self.db.write(
            lambda db: self._record_receipt(db, row["id"], row["action_id"], receipt)
        )
        telemetry.measure("theo_deliveries", outcome="succeeded", channel=telemetry_channel)
        if telemetry.enabled() and telemetry_channel == "telegram":
            job = await self.db.one(
                "SELECT j.created_at FROM actions a JOIN jobs j ON j.id=a.job_id WHERE a.id=?",
                (row["action_id"],),
            )
            if job:
                telemetry.measure(
                    "theo_telegram_delivery_duration",
                    max(0, self.db.clock() - job["created_at"]),
                    histogram=True,
                    channel="telegram",
                )
        return True

    def _job_cancelled(self, db: sqlite3.Connection, action_id: str) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM actions a JOIN jobs j ON j.id=a.job_id WHERE a.id=? AND a.owner_id=? AND j.status='cancelled'",
                (action_id, self.owner),
            ).fetchone()
            is not None
        )

    def _settle_action(self, db: sqlite3.Connection, action_id: str) -> None:
        action = db.execute(
            "SELECT a.*,o.role FROM actions a JOIN outbox o ON o.action_id=a.id WHERE a.id=? AND a.owner_id=? ORDER BY o.ordinal LIMIT 1",
            (action_id, self.owner),
        ).fetchone()
        assert action is not None
        states = {
            row[0]
            for row in db.execute("SELECT status FROM outbox WHERE action_id=?", (action_id,))
        }
        # Unresolved remote effects always remain visible, even after cancellation.
        status = next(
            (
                state
                for state in ("uncertain", "executing", "failed", "cancelled", "ready")
                if state in states
            ),
            "succeeded",
        )
        error = db.execute(
            "SELECT error FROM outbox WHERE action_id=? AND status=? AND error IS NOT NULL ORDER BY ordinal LIMIT 1",
            (action_id, status),
        ).fetchone()
        db.execute(
            "UPDATE actions SET status=?,error=?,updated_at=? WHERE id=?",
            (status, error[0] if error else None, self.db.clock(), action_id),
        )
        if (
            status == "succeeded"
            and action["role"] == "final"
            and not db.execute(
                "SELECT 1 FROM messages WHERE owner_id=? AND source=? AND role='assistant'",
                (self.owner, action_id),
            ).fetchone()
        ):
            request = json.loads(action["request"])
            self.db.append_message(
                db,
                self.owner,
                action["conversation_id"],
                "assistant",
                str(request.get("text", request.get("caption", "[artifact delivered]"))),
                self.db.clock(),
                source=action_id,
                run_id=action["run_id"],
            )

    def _record_receipt(
        self, db: sqlite3.Connection, delivery_id: str, action_id: str, receipt: Json
    ) -> None:
        db.execute(
            "UPDATE outbox SET status='succeeded',receipt=?,error=NULL WHERE id=?",
            (encode(receipt), delivery_id),
        )
        db.execute(
            "INSERT INTO delivery_receipts VALUES(?,?,?,?,?,?)",
            (
                uid(),
                self.owner,
                delivery_id,
                str(receipt.get("message_id", "")),
                encode(receipt),
                self.db.clock(),
            ),
        )
        db.execute("UPDATE actions SET receipt=? WHERE id=?", (encode(receipt), action_id))
        self._settle_action(db, action_id)
        action = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        assert action
        routing = json.loads(action["request"]).get("_telegram")
        if routing and (
            action["operation"].startswith("send_") or action["operation"] in ("reply", "forward")
        ):
            receipts = receipt.get("messages", [receipt])
            canonical = db.execute(
                "SELECT id FROM messages WHERE source=? AND role='assistant'", (action_id,)
            ).fetchone()
            for item in receipts:
                if not item.get("message_id"):
                    continue
                db.execute(
                    "INSERT INTO telegram_messages VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,bot_id,chat_id,message_id) DO UPDATE SET action_id=excluded.action_id",
                    (
                        self.owner,
                        routing["bot_id"],
                        routing["chat_id"],
                        item["message_id"],
                        routing["conversation_id"],
                        canonical[0] if canonical else None,
                        action["job_id"],
                        action_id,
                        1,
                        self.db.clock(),
                        action["request"],
                    ),
                )
                if item.get("poll"):
                    db.execute(
                        "INSERT OR REPLACE INTO telegram_polls VALUES(?,?,?,?,?,?,?)",
                        (
                            item["poll"]["id"],
                            self.owner,
                            routing["bot_id"],
                            routing["chat_id"],
                            item["message_id"],
                            routing["conversation_id"],
                            encode(item["poll"]),
                        ),
                    )

    async def reconcile(
        self,
        action_id: str,
        *,
        receipt: Json | None = None,
        confirmed_no_effect: bool = False,
        delivery_id: str | None = None,
    ) -> None:
        if receipt is None and not confirmed_no_effect:
            raise ValueError("Reconciliation requires evidence or confirmed no effect")

        def resolve(db: sqlite3.Connection) -> None:
            row = db.execute(
                "SELECT * FROM actions WHERE id=? AND owner_id=? AND status='uncertain'",
                (action_id, self.owner),
            ).fetchone()
            if row is None:
                raise Conflict("Action is not uncertain")
            ambiguous = db.execute(
                "SELECT * FROM outbox WHERE action_id=? AND status='uncertain' AND (? IS NULL OR id=?) ORDER BY ordinal",
                (action_id, delivery_id, delivery_id),
            ).fetchall()
            if len(ambiguous) != 1:
                raise Conflict(
                    "Select one uncertain chunk using its delivery_id from actions inspect"
                )
            chunk = ambiguous[0]
            if receipt is not None:
                routing = json.loads(row["request"]).get("_telegram")
                if routing and (
                    row["operation"].startswith("send_") or row["operation"] in ("reply", "forward")
                ):
                    raw_items = receipt.get("messages", [receipt])
                    expected = (
                        len(json.loads(chunk["payload"])["items"])
                        if row["operation"] == "send_media_group"
                        else 1
                    )
                    if not isinstance(raw_items, list):
                        raise Denied("Invalid Telegram delivery receipt")
                    items = cast(list[Any], raw_items)
                    if len(items) != expected:
                        raise Denied("Provide one received message ID for each delivered item")
                    if (
                        receipt.get("chat_id", routing["chat_id"]) != routing["chat_id"]
                        or receipt.get("topic_id", routing["topic_id"]) != routing["topic_id"]
                    ):
                        raise Denied("Receipt destination differs from the original action")
                    if not isinstance(items[0], dict) or receipt.get("message_id") != cast(
                        Json, items[0]
                    ).get("message_id"):
                        raise Denied("Receipt message identity is inconsistent")
                    recorded: set[int] = set()
                    for prior in db.execute(
                        "SELECT receipt FROM outbox WHERE action_id=? AND id<>? AND status='succeeded'",
                        (action_id, chunk["id"]),
                    ):
                        prior_receipt = json.loads(prior["receipt"])
                        recorded.update(
                            item["message_id"]
                            for item in prior_receipt.get("messages", [prior_receipt])
                        )
                    seen: set[int] = set()
                    for raw_item in items:
                        if not isinstance(raw_item, dict):
                            raise Denied("Invalid Telegram delivery receipt")
                        item = cast(Json, raw_item)
                        mid = item.get("message_id")
                        if type(mid) is not int or mid <= 0 or mid in seen:
                            raise Denied(
                                "Received Telegram message IDs must be positive and unique"
                            )
                        seen.add(mid)
                        if mid in recorded:
                            raise Denied("That Telegram message already confirms a different chunk")
                        if (
                            item.get("chat_id", routing["chat_id"]) != routing["chat_id"]
                            or item.get("topic_id", routing["topic_id"]) != routing["topic_id"]
                        ):
                            raise Denied("Receipt destination differs from the original action")
                        known = db.execute(
                            "SELECT action_id FROM telegram_messages WHERE owner_id=? AND bot_id=? AND chat_id=? AND message_id=?",
                            (self.owner, routing["bot_id"], routing["chat_id"], mid),
                        ).fetchone()
                        if known and known["action_id"] != action_id:
                            raise Denied("That message belongs to another Telegram input or action")
                self._record_receipt(db, chunk["id"], action_id, receipt)
            else:
                cancelled = self._job_cancelled(db, action_id)
                db.execute(
                    "UPDATE outbox SET status=?,available_at=?,error=? WHERE id=?",
                    (
                        "cancelled" if cancelled else "ready",
                        self.db.clock(),
                        "job_cancelled" if cancelled else None,
                        chunk["id"],
                    ),
                )
                self._settle_action(db, action_id)

        await self.db.write(resolve)
