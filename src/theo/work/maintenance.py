"""Canonical maintenance intents and idempotent controller event projection.

The core owns jobs and notifications; the separate controller owns publication
and activation evidence. Every model mutation is fenced in its SQL transaction.
"""

import asyncio
import json
import sqlite3

from theo.config import Settings
from theo.delivery.ledger import Delivery
from theo.domain import Conflict, Denied, Json, ToolContext, encode, uid
from theo.maintenance.contracts import MaintenanceRequest
from theo.maintenance.rpc import Client
from theo.maintenance.source import source_digest
from theo.storage import Database
from theo.work.jobs import Jobs


class Maintenance:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self.jobs = Jobs(db, settings.owner_id)

    def authorize(self, connection: sqlite3.Connection, context: ToolContext) -> sqlite3.Row:
        job = self.jobs.check(connection, context.job_id, context.generation)
        private = connection.execute(
            "SELECT 1 FROM conversations c LEFT JOIN telegram_destinations t ON t.conversation_id=c.id "
            "WHERE c.id=? AND c.owner_id=? AND (t.private IS NULL OR t.private=1)",
            (context.conversation_id, self.settings.owner_id),
        ).fetchone()
        if (
            not self.settings.maintenance_socket
            or not self.settings.maintenance_token_file
            or not self.settings.maintenance_installation_id
            or not self.settings.worker_home
            or not private
            or job["conversation_id"] != context.conversation_id
            or (
                job["origin"] != "requested"
                and not (job["origin"] == "autonomous" and self.settings.maintenance_proactive)
            )
        ):
            raise Denied("Maintenance requires an enabled private owner installation")
        return job

    async def begin(self, context: ToolContext, args: Json) -> Json:
        def begin(connection: sqlite3.Connection) -> Json:
            job = self.authorize(connection, context)
            if job["origin"] == "autonomous":
                evidence = args.get("evidence", [])
                if not evidence:
                    raise Denied("Proactive maintenance requires a current failure or active goal")
                for reference in evidence:
                    kind, _, identifier = reference.partition(":")
                    if kind == "health":
                        valid = connection.execute(
                            "SELECT 1 FROM health_events WHERE id=? AND owner_id=? AND created_at>?",
                            (identifier, context.owner_id, self.db.clock() - 86400),
                        ).fetchone()
                    elif kind == "goal":
                        valid = connection.execute(
                            "SELECT 1 FROM goals WHERE id=? AND owner_id=? AND status='active'",
                            (identifier, context.owner_id),
                        ).fetchone()
                    else:
                        valid = None
                    if not valid:
                        raise Denied(
                            "Maintenance evidence must name an owned active goal or recent health event"
                        )
            request_id = uid()
            coding = self.jobs.insert(
                connection,
                context.conversation_id,
                "maintenance_code",
                {"text": args["objective"]},
                "maintenance-code:" + request_id,
                parent=context.job_id,
                deadline=self.db.clock() + 14400,
            )
            review = self.jobs.insert(
                connection,
                context.conversation_id,
                "maintenance_review",
                {
                    "text": "Review the immutable source change independently. Inspect the diff and tests. "
                    "Use maintenance_review with the exact candidate identity and your verdict."
                },
                "maintenance-review:" + request_id,
                parent=context.job_id,
                deadline=self.db.clock() + 14400,
            )
            connection.execute(
                "UPDATE jobs SET status='waiting_for_dependency' WHERE id IN (?,?)",
                (coding, review),
            )
            request = MaintenanceRequest(
                request_id=request_id,
                owner_id=context.owner_id,
                installation_id=str(self.settings.maintenance_installation_id),
                job_id=context.job_id,
                coding_job_id=coding,
                review_job_id=review,
                conversation_id=context.conversation_id,
                origin=job["origin"],
                objective=args["objective"],
                target=args["target"],
                evidence=tuple(args.get("evidence", ())),
            )
            connection.execute(
                "INSERT INTO maintenance_intents(id,owner_id,conversation_id,coding_job_id,review_job_id,request,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    request_id,
                    context.owner_id,
                    context.conversation_id,
                    coding,
                    review,
                    encode(request.model_dump(mode="json")),
                    self.db.clock(),
                ),
            )
            connection.execute(
                "INSERT INTO maintenance_rounds VALUES(?,1,?,?,NULL,NULL)",
                (request_id, coding, review),
            )
            return {"request_id": request_id, "job_id": coding, "stage": "preparing"}

        return await self.db.write(begin)

    async def submit(self, context: ToolContext, args: Json) -> Json:
        checksum = await asyncio.to_thread(source_digest, context.workspace)

        def submit(connection: sqlite3.Connection) -> Json:
            self.authorize(connection, context)
            row = connection.execute(
                "SELECT * FROM maintenance_intents WHERE change_id=? AND coding_job_id=?",
                (args["change_id"], context.job_id),
            ).fetchone()
            if not row or row["revision"] != args["expected_revision"]:
                raise Conflict("Maintenance workspace revision changed or is not owned by this job")
            body = {
                "revision": row["revision"],
                "snapshot_sha256": checksum,
                "summary": args["summary"],
            }
            if row["submission"] and json.loads(row["submission"]) != body:
                raise Conflict("Submitted source revision is immutable")
            connection.execute(
                "UPDATE maintenance_intents SET submission=? WHERE id=?", (encode(body), row["id"])
            )
            connection.execute(
                "UPDATE maintenance_rounds SET submission=? WHERE intent_id=? AND revision=?",
                (encode(body), row["id"], row["revision"]),
            )
            return {"change_id": row["change_id"], "stage": "verifying", "submission": body}

        return await self.db.write(submit)

    async def review(self, context: ToolContext, args: Json) -> Json:
        def review(connection: sqlite3.Connection) -> Json:
            self.authorize(connection, context)
            row = connection.execute(
                "SELECT * FROM maintenance_intents WHERE change_id=? AND review_job_id=?",
                (args["change_id"], context.job_id),
            ).fetchone()
            if not row:
                raise Denied("Only the independent review job can record this review")
            projection = json.loads(row["projection"])
            if args["candidate"] != projection.get("candidate"):
                raise Conflict("Review does not bind the current immutable candidate")
            body = {**args, "job_id": context.job_id, "run_id": context.run_id}
            if row["review"] and json.loads(row["review"]) != body:
                raise Conflict("Review is immutable")
            connection.execute(
                "UPDATE maintenance_intents SET review=? WHERE id=?", (encode(body), row["id"])
            )
            connection.execute(
                "UPDATE maintenance_rounds SET review=? WHERE intent_id=? AND revision=?",
                (encode(body), row["id"], row["revision"]),
            )
            return {"recorded": True, "approved": args["approved"]}

        return await self.db.write(review)

    async def control(self, context: ToolContext, args: Json, *, rollback: bool = False) -> Json:
        def change(connection: sqlite3.Connection) -> Json:
            self.authorize(connection, context)
            row = connection.execute(
                "SELECT * FROM maintenance_intents WHERE change_id=? AND owner_id=?",
                (args["change_id"], context.owner_id),
            ).fetchone()
            if not row:
                raise Denied("Maintenance change unavailable")
            if rollback:
                connection.execute(
                    "UPDATE maintenance_intents SET rollback_requested=? WHERE id=?",
                    (args["reason"], row["id"]),
                )
            else:
                connection.execute(
                    "UPDATE maintenance_intents SET cancel_requested=1 WHERE id=?", (row["id"],)
                )
                self.jobs.cancel_in(connection, row["coding_job_id"])
                self.jobs.cancel_in(connection, row["review_job_id"])
            return {
                "change_id": row["change_id"],
                "requested": "rollback" if rollback else "cancel",
            }

        return await self.db.write(change)

    async def status(self, context: ToolContext, change_id: str | None) -> Json:
        await self.db.write(lambda connection: self.authorize(connection, context))
        rows = await self.db.read(
            "SELECT id,change_id,revision,coding_job_id,review_job_id,projection FROM maintenance_intents WHERE owner_id=? AND (? IS NULL OR change_id=?) ORDER BY created_at DESC LIMIT 20",
            (context.owner_id, change_id, change_id),
        )
        return {"changes": [{**row, "projection": json.loads(row["projection"])} for row in rows]}

    async def bridge(self) -> None:
        if not self.settings.maintenance_socket or not self.settings.maintenance_token_file:
            return
        client = Client(self.settings.maintenance_socket, self.settings.maintenance_token_file)
        for row in await self.db.read(
            "SELECT * FROM maintenance_intents WHERE owner_id=? AND (change_id IS NULL OR COALESCE(json_extract(projection,'$.stage'),'') NOT IN ('published','deployed','rolled_back','failed','cancelled') OR rollback_requested IS NOT NULL) ORDER BY created_at LIMIT 100",
            (self.settings.owner_id,),
        ):
            result = await client.call("accept", json.loads(row["request"]))
            change_id = result["id"]
            await self.db.execute(
                "UPDATE maintenance_intents SET change_id=? WHERE id=?", (change_id, row["id"])
            )
            if row["revision"] > 1:
                await client.call(
                    "signal",
                    {
                        "change_id": change_id,
                        "name": "round_jobs",
                        "body": {
                            "revision": row["revision"],
                            "coding_job_id": row["coding_job_id"],
                            "review_job_id": row["review_job_id"],
                        },
                    },
                )
            for name, body in (("submission", row["submission"]), ("review", row["review"])):
                if body:
                    await client.call(
                        "signal", {"change_id": change_id, "name": name, "body": json.loads(body)}
                    )
            cancelled = await self.db.one(
                "SELECT 1 FROM jobs WHERE id IN (?,?) AND status='cancelled'",
                (row["coding_job_id"], row["review_job_id"]),
            )
            if row["cancel_requested"] or cancelled:
                await client.call("signal", {"change_id": change_id, "name": "cancel", "body": {}})
            if row["rollback_requested"]:
                await client.call(
                    "signal",
                    {
                        "change_id": change_id,
                        "name": "rollback",
                        "body": {"reason": row["rollback_requested"]},
                    },
                )
                await self.db.execute(
                    "UPDATE maintenance_intents SET rollback_requested=NULL WHERE id=?",
                    (row["id"],),
                )
        latest = await self.db.one("SELECT COALESCE(max(sequence),0) n FROM maintenance_events")
        page = await client.call("events", {"after": latest["n"] if latest else 0})
        for event in page["events"]:

            def apply(connection: sqlite3.Connection, event: Json = event) -> None:
                row = connection.execute(
                    "SELECT * FROM maintenance_intents WHERE change_id=? AND owner_id=?",
                    (event["change_id"], self.settings.owner_id),
                ).fetchone()
                if not row:
                    raise Denied("Controller event belongs to an unknown request")
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO maintenance_events VALUES(?,?,?,?,?,?)",
                    (
                        event["sequence"],
                        event["change_id"],
                        event["revision"],
                        event["kind"],
                        encode(event["detail"]),
                        self.db.clock(),
                    ),
                ).rowcount
                if not inserted:
                    return
                detail = event["detail"]
                prior_projection: Json = json.loads(row["projection"])
                if (
                    event["kind"] == "preparing"
                    and detail.get("expected_revision", 1) > row["revision"]
                ):
                    revision = detail["expected_revision"]
                    if revision != row["revision"] + 1:
                        raise Conflict("Controller skipped a candidate revision")
                    request = MaintenanceRequest.model_validate_json(row["request"])
                    # Revoke both previous jobs before admitting their successors;
                    # retained round rows preserve the immutable submission/review.
                    self.jobs.cancel_in(connection, row["coding_job_id"])
                    self.jobs.cancel_in(connection, row["review_job_id"])
                    coding = self.jobs.insert(
                        connection,
                        row["conversation_id"],
                        "maintenance_code",
                        {"text": request.objective + "\nRepair context: " + encode(detail)},
                        f"maintenance-code:{row['id']}:{revision}",
                        parent=request.job_id,
                        deadline=min(detail["deadline"], row["created_at"] + 14400),
                    )
                    review = self.jobs.insert(
                        connection,
                        row["conversation_id"],
                        "maintenance_review",
                        {
                            "text": "Independently review this new candidate against the objective: "
                            + request.objective
                        },
                        f"maintenance-review:{row['id']}:{revision}",
                        parent=request.job_id,
                        deadline=min(detail["deadline"], row["created_at"] + 14400),
                    )
                    connection.execute(
                        "UPDATE jobs SET status='waiting_for_dependency' WHERE id IN (?,?)",
                        (coding, review),
                    )
                    connection.execute(
                        "INSERT INTO maintenance_rounds VALUES(?,?,?,?,NULL,NULL)",
                        (row["id"], revision, coding, review),
                    )
                    connection.execute(
                        "UPDATE maintenance_intents SET revision=?,coding_job_id=?,review_job_id=?,submission=NULL,review=NULL WHERE id=?",
                        (revision, coding, review, row["id"]),
                    )
                    prior_projection = {}
                projection = {
                    **prior_projection,
                    **detail,
                    "stage": prior_projection.get("stage", "preparing")
                    if event["kind"] in {"claimed", "cancel_requested"}
                    else event["kind"],
                    "event_revision": event["revision"],
                }
                connection.execute(
                    "UPDATE maintenance_intents SET projection=? WHERE id=?",
                    (encode(projection), row["id"]),
                )
                job_id = (
                    row["coding_job_id"]
                    if event["kind"] == "editing"
                    else row["review_job_id"]
                    if detail.get("review_ready")
                    else None
                )
                if job_id:
                    job = connection.execute(
                        "SELECT payload FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    assert job
                    payload = json.loads(job["payload"])
                    payload["text"] += (
                        "\nMaintenance change: " + event["change_id"] + "\n" + encode(detail)
                    )
                    connection.execute(
                        "UPDATE jobs SET status='queued',payload=? WHERE id=? AND status='waiting_for_dependency'",
                        (encode(payload), job_id),
                    )
                if event["kind"] in {
                    "published",
                    "deployed",
                    "rolled_back",
                    "failed",
                    "cancelled",
                } or detail.get("status") in {"blocked", "uncertain", "auth_wait", "quota_wait"}:
                    Delivery(self.db, self.settings).prepare_in(
                        connection,
                        row["conversation_id"],
                        "send_message",
                        {
                            "text": "Maintenance "
                            + event["change_id"]
                            + ": "
                            + event["kind"]
                            + ".\n"
                            + encode(detail)
                        },
                        "maintenance-event:" + str(event["sequence"]),
                        role="progress",
                        durable_obligation=True,
                    )

            await self.db.write(apply)
