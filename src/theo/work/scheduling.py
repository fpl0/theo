"""Persist and advance reminders with explicit local-time recurrence.

Handles DST, bounded catch-up and schedule changes, then creates durable jobs
or delivery obligations when due without invoking a reasoning model.
"""

import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from theo.domain import Conflict, digest, uid
from theo.storage import Database
from theo.work.jobs import Jobs


def next_cron(expression: str, timezone: str, after: float) -> float:
    zone = ZoneInfo(timezone)
    local = datetime.fromtimestamp(after, zone).replace(tzinfo=None)
    iterator = croniter(expression, local)
    for _ in range(10000):
        nominal: datetime = iterator.get_next(datetime)
        candidate = nominal.replace(tzinfo=zone, fold=0)
        round_trip = candidate.astimezone(UTC).astimezone(zone)
        if round_trip.replace(tzinfo=None) != nominal:
            continue  # nonexistent local time: skip, never slide the user's intent
        if candidate.timestamp() > after:
            return candidate.timestamp()  # fold=0 selects the earlier repeated instant
    raise ValueError("Could not find a valid recurrence within the search bound")


class Scheduler:
    def __init__(self, db: Database, owner: str):
        self.db, self.owner = db, owner
        self.jobs = Jobs(db, owner)

    async def create(
        self,
        conversation: str,
        body: str,
        *,
        due: float | None = None,
        cron: str | None = None,
        interval: int | None = None,
        timezone: str = "Europe/Dublin",
        idempotency_key: str | None = None,
    ) -> str:
        ZoneInfo(timezone)
        if sum(value is not None for value in (due, cron, interval)) != 1 or not body.strip():
            raise ValueError("Specify exactly one due time, cron expression or interval")
        if interval is not None and interval < 60:
            raise ValueError("Minimum maintenance interval is 60 seconds")
        if cron:
            next_due = next_cron(cron, timezone, self.db.clock())
            kind = "cron"
        elif interval:
            next_due, kind = self.db.clock() + interval, "interval"
        else:
            assert due is not None
            next_due, kind = due, "once"
        schedule_id = (
            digest({"owner": self.owner, "conversation": conversation, "key": idempotency_key})
            if idempotency_key
            else uid()
        )

        def insert(db: sqlite3.Connection) -> None:
            existing = db.execute(
                "SELECT body,kind,cron,interval_seconds,timezone FROM schedules WHERE id=?",
                (schedule_id,),
            ).fetchone()
            if existing:
                if tuple(existing) != (body, kind, cron, interval, timezone):
                    raise Conflict("Reminder identity binds different content")
                return
            if not db.execute(
                "SELECT 1 FROM conversations WHERE id=? AND owner_id=?", (conversation, self.owner)
            ).fetchone():
                raise ValueError("Conversation unavailable")
            db.execute(
                "INSERT INTO schedules(id,owner_id,conversation_id,body,kind,cron,interval_seconds,timezone,next_due,active,grace_seconds,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    schedule_id,
                    self.owner,
                    conversation,
                    body,
                    kind,
                    cron,
                    interval,
                    timezone,
                    next_due,
                    1,
                    3600,
                    self.db.clock(),
                ),
            )

        await self.db.write(insert)
        return schedule_id

    async def reschedule(
        self, schedule_id: str, due: float, conversation: str | None = None
    ) -> None:
        if due <= self.db.clock():
            raise ValueError("Choose a future time")

        def change(db: sqlite3.Connection) -> None:
            row = db.execute(
                "SELECT * FROM schedules WHERE id=? AND owner_id=? AND (? IS NULL OR conversation_id=?)",
                (schedule_id, self.owner, conversation, conversation),
            ).fetchone()
            if not row:
                raise ValueError("Schedule unavailable")
            db.execute("UPDATE schedules SET next_due=?,active=1 WHERE id=?", (due, schedule_id))
            for occurrence in db.execute(
                "SELECT job_id FROM occurrences WHERE schedule_id=?", (schedule_id,)
            ).fetchall():
                db.execute(
                    "UPDATE jobs SET status='cancelled',generation=generation+1 WHERE id=? AND status IN ('queued','interrupted')",
                    (occurrence[0],),
                )
                db.execute(
                    "UPDATE actions SET status='cancelled' WHERE job_id=? AND status IN ('ready','awaiting_approval')",
                    (occurrence[0],),
                )
                db.execute(
                    "UPDATE outbox SET status='cancelled' WHERE action_id IN (SELECT id FROM actions WHERE job_id=? AND status='cancelled') AND status='ready'",
                    (occurrence[0],),
                )

        await self.db.write(change)

    async def deliver_reminders(self, settings: object) -> int:
        from theo.config import Settings
        from theo.delivery.ledger import Delivery
        from theo.domain import encode

        if not isinstance(settings, Settings):
            raise TypeError("Validated Settings required")

        def deliver(db: sqlite3.Connection) -> int:
            rows = db.execute(
                "SELECT * FROM jobs WHERE owner_id=? AND kind='reminder' AND status IN ('queued','interrupted') AND available_at<=?",
                (self.owner, self.db.clock()),
            ).fetchall()
            for row in rows:
                import json

                payload = json.loads(row["payload"])
                text = payload["text"]
                if payload.get("late_seconds", 0) > 3600:
                    text += "\n(This reminder is late because Theo was unavailable.)"
                Delivery(self.db, settings).prepare_in(
                    db,
                    row["conversation_id"],
                    "send_message",
                    {"text": text},
                    "final:" + row["id"],
                    job_id=row["id"],
                    role="final",
                    autonomous=True,
                    durable_obligation=True,
                )
                db.execute(
                    "UPDATE jobs SET status='completed',outcome=?,updated_at=? WHERE id=?",
                    (encode({"delivery": "queued", "reminder": True}), self.db.clock(), row["id"]),
                )
            return len(rows)

        return await self.db.write(deliver)

    async def tick(self) -> list[str]:
        def admit(db: sqlite3.Connection) -> list[str]:
            jobs: list[str] = []
            rows = db.execute(
                "SELECT * FROM schedules WHERE owner_id=? AND active=1 AND next_due<=?",
                (self.owner, self.db.clock()),
            ).fetchall()
            for row in rows:
                nominal = row["next_due"]
                eligible = True
                if row["kind"] == "cron":
                    # Find at most the last hour's latest slot; never iterate a two-week backlog.
                    candidate = next_cron(
                        row["cron"],
                        row["timezone"],
                        max(nominal - 1, self.db.clock() - row["grace_seconds"]),
                    )
                    eligible = candidate <= self.db.clock()
                    while candidate <= self.db.clock():
                        nominal = candidate
                        candidate = next_cron(row["cron"], row["timezone"], candidate)
                    next_due = candidate
                    db.execute("UPDATE schedules SET next_due=? WHERE id=?", (next_due, row["id"]))
                elif row["kind"] == "interval":
                    interval = row["interval_seconds"]
                    nominal += int((self.db.clock() - nominal) // interval) * interval
                    db.execute(
                        "UPDATE schedules SET next_due=? WHERE id=?",
                        (nominal + interval, row["id"]),
                    )
                else:
                    db.execute("UPDATE schedules SET active=0 WHERE id=?", (row["id"],))
                if not eligible:
                    continue
                key = f"schedule:{row['id']}:{nominal}"
                job = self.jobs.insert(
                    db,
                    row["conversation_id"],
                    "reminder",
                    {
                        "text": row["body"],
                        "nominal_due": nominal,
                        "schedule_id": row["id"],
                        "late_seconds": max(0, self.db.clock() - nominal),
                    },
                    key,
                    deadline=self.db.clock() + 7 * 86400,
                )
                db.execute(
                    "INSERT OR IGNORE INTO occurrences VALUES(?,?,?,?,?)",
                    (uid(), self.owner, row["id"], nominal, job),
                )
                jobs.append(job)
            return jobs

        return await self.db.write(admit)

    async def cancel(self, schedule_id: str) -> None:
        await self.db.execute(
            "UPDATE schedules SET active=0 WHERE id=? AND owner_id=?", (schedule_id, self.owner)
        )
