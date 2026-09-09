"""Durable job admission, leasing, cancellation and crash recovery.

Owns queue identities, parent relationships, deadlines and fencing generations.
Native execution and user-facing delivery are coordinated by the application.
"""

import sqlite3

from theo.domain import Conflict, Denied, Json, Outcome, encode, uid
from theo.observability import telemetry
from theo.storage import Database


class Jobs:
    def __init__(self, db: Database, owner: str):
        self.db, self.owner = db, owner

    def insert(
        self,
        db: sqlite3.Connection,
        conversation: str,
        kind: str,
        payload: Json,
        key: str,
        *,
        lane: str = "background",
        parent: str | None = None,
        deadline: float | None = None,
        available: float | None = None,
    ) -> str:
        conv = db.execute(
            "SELECT id FROM conversations WHERE id=? AND owner_id=?", (conversation, self.owner)
        ).fetchone()
        if conv is None:
            raise Denied("Conversation unavailable")
        parent_row = (
            db.execute(
                "SELECT root_id FROM jobs WHERE id=? AND owner_id=?", (parent, self.owner)
            ).fetchone()
            if parent
            else None
        )
        if parent and parent_row is None:
            raise Denied("Parent unavailable")
        job_id, timestamp = uid(), self.db.clock()
        db.execute(
            "INSERT OR IGNORE INTO jobs(id,owner_id,conversation_id,parent_id,root_id,kind,lane,status,payload,semantic_key,deadline,available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                self.owner,
                conversation,
                parent,
                parent_row[0] if parent_row else job_id,
                kind,
                lane,
                "queued",
                encode(payload),
                key,
                deadline or timestamp + 1800,
                available if available is not None else timestamp,
                timestamp,
                timestamp,
            ),
        )
        row = db.execute(
            "SELECT id,payload,kind FROM jobs WHERE owner_id=? AND semantic_key=?",
            (self.owner, key),
        ).fetchone()
        assert row is not None
        if row["payload"] != encode(payload) or row["kind"] != kind:
            raise Conflict("Job identity already binds a different request")
        traceparent = telemetry.carrier()
        if traceparent:
            db.execute(
                "INSERT OR IGNORE INTO telemetry_links VALUES(?,?,?,?)",
                ("job", row["id"], traceparent, timestamp),
            )
        return str(row["id"])

    async def enqueue(
        self,
        conversation: str,
        kind: str,
        payload: Json,
        key: str,
        *,
        lane: str = "background",
        parent: str | None = None,
        deadline: float | None = None,
        available: float | None = None,
    ) -> str:
        return await self.db.write(
            lambda db: self.insert(
                db,
                conversation,
                kind,
                payload,
                key,
                lane=lane,
                parent=parent,
                deadline=deadline,
                available=available,
            )
        )

    @telemetry.observed("channel.ingest")
    async def ingest(
        self,
        conversation: str,
        channel: str,
        update_id: str,
        payload: Json,
        text: str,
        parts: list[Json] | None = None,
        *,
        require_idle: bool = False,
    ) -> str | None:
        def commit(db: sqlite3.Connection) -> str | None:
            changed = db.execute(
                "INSERT OR IGNORE INTO inbox_updates VALUES(?,?,?,?,?)",
                (self.owner, channel, update_id, encode(payload), self.db.clock()),
            ).rowcount
            if not changed:
                return None
            if (
                require_idle
                and db.execute(
                    "SELECT 1 FROM jobs WHERE owner_id=? AND conversation_id=? AND status NOT IN ('completed','failed','cancelled') LIMIT 1",
                    (self.owner, conversation),
                ).fetchone()
            ):
                raise Denied("This conversation has unfinished work. Use /wait or /cancel first.")
            message = self.db.append_message(
                db,
                self.owner,
                conversation,
                "user",
                text,
                self.db.clock(),
                source=f"{channel}:{update_id}",
                parts=encode(parts or []),
            )
            return self.insert(
                db,
                conversation,
                "conversation",
                {"text": text, "message_id": message, "parts": parts or []},
                f"inbox:{channel}:{update_id}",
                lane="interactive",
            )

        return await self.db.write(commit)

    async def claim(
        self,
        lane: str,
        worker: str,
        *,
        max_total: int = 2,
        max_background: int = 1,
        reminders_only: bool = False,
    ) -> Json | None:
        def claim(db: sqlite3.Connection) -> Json | None:
            paused = db.execute(
                "SELECT value FROM control WHERE owner_id=? AND key='models_paused'",
                (self.owner,),
            ).fetchone()
            only_reminders = reminders_only or bool(paused and paused[0] == "true")
            active = db.execute(
                "SELECT lane,count(*) n FROM jobs WHERE owner_id=? AND status='running' GROUP BY lane",
                (self.owner,),
            ).fetchall()
            counts = {row["lane"]: row["n"] for row in active}
            if sum(counts.values()) >= max_total or (
                lane == "background" and counts.get("background", 0) >= max_background
            ):
                return None
            row = db.execute(
                "SELECT j.* FROM jobs j WHERE j.owner_id=? AND j.lane=? AND j.status IN ('queued','interrupted') AND j.available_at<=? AND (?=0 OR j.kind='reminder') AND NOT EXISTS(SELECT 1 FROM jobs x WHERE x.conversation_id=j.conversation_id AND x.status='running') AND NOT EXISTS(SELECT 1 FROM job_dependencies d JOIN jobs p ON p.id=d.depends_on WHERE d.job_id=j.id AND p.status<>'completed') ORDER BY j.created_at LIMIT 1",
                (self.owner, lane, self.db.clock(), int(only_reminders)),
            ).fetchone()
            if row is None:
                return None
            if row["deadline"] <= self.db.clock():
                db.execute(
                    "UPDATE jobs SET status='failed',outcome=?,updated_at=? WHERE id=?",
                    (encode({"error": "deadline_expired"}), self.db.clock(), row["id"]),
                )
                return None
            db.execute(
                "UPDATE jobs SET status='running',generation=generation+1,attempts=attempts+1,worker=?,lease_until=?,updated_at=? WHERE id=?",
                (worker, self.db.clock() + 60, self.db.clock(), row["id"]),
            )
            claimed = db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            assert claimed is not None
            return dict(claimed)

        return await self.db.write(claim)

    def check(self, db: sqlite3.Connection, job_id: str, generation: int) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM jobs WHERE id=? AND owner_id=?", (job_id, self.owner)
        ).fetchone()
        if (
            row is None
            or row["status"] != "running"
            or row["generation"] != generation
            or row["lease_until"] <= self.db.clock()
        ):
            raise Denied("Job lease revoked or stale")
        return row

    async def heartbeat(self, job_id: str, generation: int) -> None:
        def beat(db: sqlite3.Connection) -> None:
            self.check(db, job_id, generation)
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (self.db.clock() + 60, job_id))
            db.execute(
                "UPDATE resource_claims SET lease_until=? WHERE job_id=? AND generation=?",
                (self.db.clock() + 60, job_id, generation),
            )

        await self.db.write(beat)

    async def finish(self, job_id: str, generation: int, status: Outcome, outcome: Json) -> None:
        def finish(db: sqlite3.Connection) -> None:
            self.check(db, job_id, generation)
            db.execute(
                "UPDATE jobs SET status=?,outcome=?,lease_until=NULL,updated_at=? WHERE id=?",
                (status.value, encode(outcome), self.db.clock(), job_id),
            )
            db.execute(
                "DELETE FROM resource_claims WHERE job_id=? AND generation=?", (job_id, generation)
            )

        await self.db.write(finish)

    async def cancel(self, job_id: str) -> list[str]:
        def cancel(db: sqlite3.Connection) -> list[str]:
            rows = db.execute(
                "WITH RECURSIVE descendants(id) AS (SELECT id FROM jobs WHERE id=? AND owner_id=? UNION ALL SELECT j.id FROM jobs j JOIN descendants d ON j.parent_id=d.id) SELECT id FROM descendants",
                (job_id, self.owner),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE jobs SET status='cancelled',generation=generation+1,lease_until=NULL,updated_at=? WHERE id=? AND status NOT IN ('completed','failed','cancelled')",
                    (self.db.clock(), row[0]),
                )
                db.execute(
                    "UPDATE actions SET status='cancelled' WHERE job_id=? AND status IN ('prepared','ready','awaiting_approval')",
                    (row[0],),
                )
                db.execute(
                    "UPDATE outbox SET status='cancelled',error='job_cancelled' WHERE action_id IN (SELECT id FROM actions WHERE job_id=?) AND status='ready'",
                    (row[0],),
                )
                db.execute("DELETE FROM resource_claims WHERE job_id=?", (row[0],))
            return [str(x[0]) for x in rows]

        return await self.db.write(cancel)

    async def recover(self) -> Json:
        # Called only after exclusive daemon lock and old runtime process termination.
        def recover(db: sqlite3.Connection) -> Json:
            ambiguous = db.execute(
                "UPDATE actions SET status='uncertain',error='dispatch_interrupted' WHERE owner_id=? AND status='executing'",
                (self.owner,),
            ).rowcount
            db.execute(
                "UPDATE outbox SET status='uncertain',error='dispatch_interrupted' WHERE owner_id=? AND status='executing'",
                (self.owner,),
            )
            interrupted = db.execute(
                "UPDATE jobs SET status=CASE WHEN EXISTS(SELECT 1 FROM actions a WHERE a.job_id=jobs.id AND a.status='uncertain') THEN 'uncertain' WHEN attempts>=3 THEN 'failed' ELSE 'interrupted' END,generation=generation+1,lease_until=NULL,updated_at=? WHERE owner_id=? AND status='running'",
                (self.db.clock(), self.owner),
            ).rowcount
            db.execute(
                "UPDATE runs SET status='interrupted',ended_at=? WHERE owner_id=? AND status='running'",
                (self.db.clock(), self.owner),
            )
            db.execute("DELETE FROM resource_claims WHERE owner_id=?", (self.owner,))
            return {"interrupted_jobs": interrupted, "uncertain_actions": ambiguous}

        return await self.db.write(recover)

    async def resource(self, job_id: str, generation: int, resource: str) -> None:
        def acquire(db: sqlite3.Connection) -> None:
            self.check(db, job_id, generation)
            row = db.execute(
                "SELECT * FROM resource_claims WHERE resource=?", (resource,)
            ).fetchone()
            if row and (row["job_id"], row["generation"]) != (job_id, generation):
                raise Conflict("Resource has an unresolved owner")
            db.execute(
                "INSERT OR REPLACE INTO resource_claims VALUES(?,?,?,?,?)",
                (resource, self.owner, job_id, generation, self.db.clock() + 60),
            )

        await self.db.write(acquire)
