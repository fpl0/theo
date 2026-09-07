"""Transactional intent ledger; remote uncertainty is explicit, never blindly retried."""

import json
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from theo.config import Settings
from theo.domain import Conflict, Denied, Json, digest, encode, uid
from theo.jobs import Jobs
from theo.storage import Database

type Sender = Callable[[str, Json], Awaitable[Json]]


class NoEffect(Exception):
    """The transport has positive evidence the request was rejected before effect."""

    def __init__(self, reason: str, retry_after: float | None = None):
        super().__init__(reason)
        self.retry_after = retry_after


def split_text(text: str, limit: int = 4096) -> list[str]:
    # Plain text deliberately uses no parse_mode: literal HTML cannot break markup or inject links.
    if not text:
        raise ValueError("Text cannot be empty")
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for character in text:
        width = len(character.encode("utf-16-le")) // 2
        if units + width > limit:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(character)
        units += width
    if current:
        chunks.append("".join(current))
    return chunks


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
    ) -> str:
        conv = db.execute(
            "SELECT * FROM conversations WHERE id=? AND owner_id=?", (conversation, self.owner)
        ).fetchone()
        if conv is None:
            raise Denied("Conversation unavailable")
        if job_id and generation is not None:
            Jobs(self.db, self.owner).check(db, job_id, generation)
        recipient = target or str(conv["target"])
        external = recipient != str(conv["target"]) or require_approval
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
        elif "caption" in request and len(str(request["caption"]).encode("utf-16-le")) // 2 > 1024:
            caption = str(request["caption"])
            chunks = [
                {**request, "caption": ""},
                *[{"text": x, "_operation": "send_message"} for x in split_text(caption)],
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
                if reason in ("expired", "new_owner_input", "stale_fact", "authorization_missing"):
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
        channel = await self.db.one(
            "SELECT channel FROM conversations WHERE id=? AND owner_id=?",
            (row["conversation_id"], self.owner),
        )
        try:
            receipt = await send(
                operation,
                {
                    **payload,
                    "target": row["target"],
                    "_channel": channel["channel"] if channel else "local",
                },
            )
        except NoEffect as exc:
            retry = exc.retry_after is not None and row["attempts"] < 6
            retry_after, error_text = exc.retry_after, str(exc)
            await self.db.write(
                lambda db: (
                    db.execute(
                        "UPDATE outbox SET status=?,available_at=?,error=? WHERE id=?",
                        (
                            "ready" if retry else "failed",
                            self.db.clock() + max(retry_after or 0, 1),
                            error_text,
                            row["id"],
                        ),
                    ),
                    db.execute(
                        "UPDATE actions SET status=?,error=? WHERE id=?",
                        ("ready" if retry else "failed", error_text, row["action_id"]),
                    ),
                )
            )
            return True
        except BaseException as exc:
            error_name = type(exc).__name__
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

        def record(db: sqlite3.Connection) -> None:
            db.execute(
                "UPDATE outbox SET status='succeeded',receipt=?,error=NULL WHERE id=?",
                (encode(receipt), row["id"]),
            )
            db.execute(
                "INSERT INTO delivery_receipts VALUES(?,?,?,?,?,?)",
                (
                    uid(),
                    self.owner,
                    row["id"],
                    str(receipt.get("message_id", "")),
                    encode(receipt),
                    self.db.clock(),
                ),
            )
            remaining = db.execute(
                "SELECT count(*) FROM outbox WHERE action_id=? AND status<>'succeeded'",
                (row["action_id"],),
            ).fetchone()
            assert remaining
            db.execute(
                "UPDATE actions SET status=?,receipt=?,updated_at=? WHERE id=?",
                (
                    "ready" if remaining[0] else "succeeded",
                    encode(receipt),
                    self.db.clock(),
                    row["action_id"],
                ),
            )
            if not remaining[0] and row["role"] == "final":
                request = json.loads(row["request"])
                self.db.append_message(
                    db,
                    self.owner,
                    row["conversation_id"],
                    "assistant",
                    str(request.get("text", request.get("caption", "[artifact delivered]"))),
                    self.db.clock(),
                    source=row["action_id"],
                    run_id=row["run_id"],
                )

        await self.db.write(record)
        return True

    async def reconcile(
        self, action_id: str, *, receipt: Json | None = None, confirmed_no_effect: bool = False
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
                "SELECT * FROM outbox WHERE action_id=? AND status='uncertain'", (action_id,)
            ).fetchall()
            if len(ambiguous) != 1:
                raise Conflict("Reconcile individual uncertain chunks")
            chunk = ambiguous[0]
            if receipt is not None:
                db.execute(
                    "UPDATE outbox SET status='succeeded',receipt=? WHERE id=?",
                    (encode(receipt), chunk["id"]),
                )
                db.execute(
                    "INSERT OR IGNORE INTO delivery_receipts VALUES(?,?,?,?,?,?)",
                    (
                        uid(),
                        self.owner,
                        chunk["id"],
                        str(receipt.get("message_id", "")),
                        encode(receipt),
                        self.db.clock(),
                    ),
                )
                pending = db.execute(
                    "SELECT 1 FROM outbox WHERE action_id=? AND status<>'succeeded'", (action_id,)
                ).fetchone()
                db.execute(
                    "UPDATE actions SET status=?,receipt=?,error=NULL WHERE id=?",
                    ("ready" if pending else "succeeded", encode(receipt), action_id),
                )
            else:
                db.execute(
                    "UPDATE outbox SET status='ready',available_at=? WHERE id=?",
                    (self.db.clock(), chunk["id"]),
                )
                db.execute("UPDATE actions SET status='ready',error=NULL WHERE id=?", (action_id,))

        await self.db.write(resolve)
