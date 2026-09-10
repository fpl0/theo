"""Controller-owned SQLite state that survives application downtime.

Provides idempotent request admission, immutable candidate identities, fenced
stage transitions and a replayable event stream. External work runs outside writes.
"""

import json
import sqlite3
from pathlib import Path

from theo.domain import Clock, Conflict, Denied, Json, digest, encode, now, uid
from theo.maintenance.contracts import (
    CandidateIdentity,
    ControllerLease,
    ControllerStatus,
    MaintenanceRequest,
    RoundJobs,
    Stage,
    round_effect,
)
from theo.maintenance.policy import MaintenancePolicy
from theo.storage import Database

TERMINAL = frozenset({"published", "deployed", "rolled_back", "failed", "cancelled"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "preparing": frozenset({"editing"}),
    "editing": frozenset({"verifying"}),
    "verifying": frozenset({"editing", "publishing"}),
    "publishing": frozenset({"awaiting_ci", "published"}),
    "awaiting_ci": frozenset({"editing", "merging"}),
    "merging": frozenset({"packaging"}),
    "packaging": frozenset({"staged"}),
    "staged": frozenset({"draining"}),
    "draining": frozenset({"staged", "activating"}),
    "activating": frozenset({"checking", "rolled_back"}),
    "checking": frozenset({"observing", "rolled_back"}),
    "observing": frozenset({"deployed", "rolled_back"}),
}


class Journal(Database):
    def __init__(self, root: Path, clock: Clock = now):
        super().__init__(root, clock)
        self.path = self.root / "maintenance.sqlite3"

    async def initialize(self, owner: str = "owner", timezone: str = "Europe/Dublin") -> None:
        await self.migrate(Path(__file__).parent / "migrations")

    def _event(
        self, connection: sqlite3.Connection, change_id: str, kind: str, detail: Json
    ) -> None:
        row = connection.execute("SELECT revision FROM changes WHERE id=?", (change_id,)).fetchone()
        assert row is not None
        connection.execute(
            "INSERT INTO events(change_id,revision,kind,detail,created_at) VALUES(?,?,?,?,?)",
            (change_id, row["revision"], kind, encode(detail), self.clock()),
        )

    async def accept(self, policy: MaintenancePolicy, request: MaintenanceRequest) -> Json:
        policy.authorize(request)
        body = request.model_dump(mode="json")
        request_hash = digest(body)

        def accept(connection: sqlite3.Connection) -> Json:
            previous = connection.execute(
                "SELECT * FROM changes WHERE request_id=?", (request.request_id,)
            ).fetchone()
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    raise Conflict("Request identity already binds different maintenance work")
                return dict(previous)
            if request.origin == "autonomous" and request.target == "deploy":
                count = connection.execute(
                    "SELECT count(*) FROM changes WHERE json_extract(request,'$.origin')='autonomous' AND json_extract(request,'$.target')='deploy' AND created_at>?",
                    (self.clock() - 86400,),
                ).fetchone()
                if count and count[0] >= policy.proactive_deployments_per_day:
                    raise Denied("Proactive deployment admission limit reached for the past day")
            change_id, timestamp = uid(), self.clock()
            connection.execute(
                "INSERT INTO changes(id,request_id,request_hash,request,policy_hash,policy_revision,stage,status,deadline,created_at,updated_at) VALUES(?,?,?,?,?,?,'preparing','queued',?,?,?)",
                (
                    change_id,
                    request.request_id,
                    request_hash,
                    encode(body),
                    policy.fingerprint,
                    policy.revision,
                    timestamp + policy.operation_seconds,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO candidate_rounds VALUES(?,1,NULL,?,?,'initial')",
                (change_id, request.coding_job_id, request.review_job_id),
            )
            self._event(connection, change_id, "accepted", {"target": request.target})
            row = connection.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
            assert row is not None
            return dict(row)

        return await self.write(accept)

    async def claim(self, worker_id: str, *, lease_seconds: int = 60) -> ControllerLease | None:
        if not 5 <= lease_seconds <= 300:
            raise ValueError("Controller lease must be between 5 and 300 seconds")

        def claim(connection: sqlite3.Connection) -> ControllerLease | None:
            row = connection.execute(
                "SELECT * FROM changes WHERE status='queued' OR (status='working' AND lease_until<=?) ORDER BY created_at,id LIMIT 1",
                (self.clock(),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE changes SET status='working',generation=generation+1,revision=revision+1,worker_id=?,lease_until=?,updated_at=? WHERE id=?",
                (worker_id, self.clock() + lease_seconds, self.clock(), row["id"]),
            )
            self._event(connection, row["id"], "claimed", {"generation": row["generation"] + 1})
            return ControllerLease(
                change_id=row["id"],
                generation=row["generation"] + 1,
                revision=row["revision"] + 1,
                worker_id=worker_id,
                stage=row["stage"],
            )

        return await self.write(claim)

    def check(self, connection: sqlite3.Connection, lease: ControllerLease) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM changes WHERE id=?", (lease.change_id,)).fetchone()
        if (
            row is None
            or row["status"] != "working"
            or row["worker_id"] != lease.worker_id
            or row["generation"] != lease.generation
            or row["revision"] != lease.revision
            or row["lease_until"] <= self.clock()
        ):
            raise Denied("Controller lease is stale or revoked")
        return row

    async def heartbeat(self, lease: ControllerLease) -> None:
        def beat(connection: sqlite3.Connection) -> None:
            self.check(connection, lease)
            connection.execute(
                "UPDATE changes SET lease_until=? WHERE id=?", (self.clock() + 60, lease.change_id)
            )

        await self.write(beat)

    async def transition(
        self,
        lease: ControllerLease,
        stage: Stage,
        *,
        status: ControllerStatus = "queued",
        detail: Json | None = None,
        blocker: str | None = None,
    ) -> Json:
        """Only controller services call this after observing the stage's real outcome."""
        if len(encode(detail or {}).encode()) > 65536:
            raise ValueError("Store large verification logs outside the journal event")

        def move(connection: sqlite3.Connection) -> Json:
            row = self.check(connection, lease)
            if row["stage"] in TERMINAL:
                raise Conflict("Maintenance already has a terminal result")
            if (
                stage != row["stage"]
                and stage not in TRANSITIONS[row["stage"]]
                and stage not in {"failed", "cancelled"}
            ):
                raise Conflict("Invalid maintenance stage transition")
            if (
                stage == "verifying"
                and not connection.execute(
                    "SELECT 1 FROM candidates WHERE change_id=?", (lease.change_id,)
                ).fetchone()
            ):
                raise Denied("Verification requires an accepted immutable candidate")
            if stage in TERMINAL:
                next_status = "terminal"
            else:
                next_status = status
                if status in ("working", "terminal"):
                    raise ValueError("A stage must release its lease before further work")
            connection.execute(
                "UPDATE changes SET stage=?,status=?,revision=revision+1,worker_id=NULL,lease_until=NULL,blocker=?,result=?,updated_at=? WHERE id=?",
                (stage, next_status, blocker, encode(detail or {}), self.clock(), lease.change_id),
            )
            self._event(
                connection, lease.change_id, stage, {"status": next_status, **(detail or {})}
            )
            updated = connection.execute(
                "SELECT * FROM changes WHERE id=?", (lease.change_id,)
            ).fetchone()
            assert updated is not None
            return dict(updated)

        return await self.write(move)

    async def record_candidate(self, lease: ControllerLease, candidate: CandidateIdentity) -> None:
        def record(connection: sqlite3.Connection) -> None:
            row = self.check(connection, lease)
            if candidate.change_id != lease.change_id or row["stage"] != "editing":
                raise Denied("Candidate does not belong to the editing operation")
            prior = connection.execute(
                "SELECT identity FROM candidates WHERE change_id=? AND revision=?",
                (candidate.change_id, candidate.revision),
            ).fetchone()
            value = encode(candidate.model_dump(mode="json"))
            if prior:
                if prior["identity"] != value:
                    raise Conflict("Accepted candidate identities are immutable")
                return
            latest = connection.execute(
                "SELECT COALESCE(max(revision),0) FROM candidates WHERE change_id=?",
                (candidate.change_id,),
            ).fetchone()
            assert latest is not None
            issued = connection.execute(
                "SELECT revision,base_commit FROM candidate_rounds WHERE change_id=? ORDER BY revision DESC LIMIT 1",
                (lease.change_id,),
            ).fetchone()
            if (
                not issued
                or candidate.revision != issued["revision"]
                or candidate.revision != latest[0] + 1
            ):
                raise Conflict("Candidate revision must follow the last accepted revision")
            if issued["base_commit"] and candidate.base_commit != issued["base_commit"]:
                raise Conflict("Candidate base differs from the issued workspace")
            connection.execute(
                "INSERT INTO candidates VALUES(?,?,?,?)",
                (candidate.change_id, candidate.revision, value, self.clock()),
            )

        await self.write(record)

    async def current_round(self, change_id: str) -> Json:
        row = await self.one(
            "SELECT * FROM candidate_rounds WHERE change_id=? ORDER BY revision DESC LIMIT 1",
            (change_id,),
        )
        if not row:
            raise Denied("Maintenance round is missing")
        return row

    async def bind_base(self, lease: ControllerLease, revision: int, base: str) -> str:
        def bind(connection: sqlite3.Connection) -> str:
            self.check(connection, lease)
            connection.execute(
                "UPDATE candidate_rounds SET base_commit=? WHERE change_id=? AND revision=? AND base_commit IS NULL",
                (base, lease.change_id, revision),
            )
            row = connection.execute(
                "SELECT base_commit FROM candidate_rounds WHERE change_id=? AND revision=?",
                (lease.change_id, revision),
            ).fetchone()
            if not row or not row[0]:
                raise Denied("Maintenance round is missing")
            return str(row[0])

        return await self.write(bind)

    async def revise(
        self,
        lease: ControllerLease,
        candidate: CandidateIdentity,
        base: str,
        reason: str,
        policy: MaintenancePolicy,
    ) -> None:
        """Fence old execution and request fresh core-owned jobs in one durable write."""

        def revise(connection: sqlite3.Connection) -> None:
            row = self.check(connection, lease)
            request = MaintenanceRequest.model_validate_json(row["request"])
            policy.authorize(request)
            if row["cancel_requested"] or row["policy_hash"] != policy.fingerprint:
                raise Denied("Maintenance authority changed before repair admission")
            if self.clock() >= row["deadline"]:
                raise Denied("Maintenance operation deadline reached")
            if (
                request.origin == "autonomous"
                and candidate.revision >= policy.max_candidate_revisions
            ):
                raise Denied("Proactive candidate revision limit reached")
            current = connection.execute(
                "SELECT identity FROM candidates WHERE change_id=? ORDER BY revision DESC LIMIT 1",
                (lease.change_id,),
            ).fetchone()
            if (
                row["stage"] not in {"verifying", "awaiting_ci", "merging"}
                or not current
                or json.loads(current[0]) != candidate.model_dump(mode="json")
            ):
                raise Conflict("Only the current unmerged candidate can be revised")
            # Resolve a lost merge acknowledgement before allowing another push.
            merging = connection.execute(
                "SELECT retryable FROM effects WHERE change_id=? AND name=?",
                (lease.change_id, round_effect(candidate.revision, "merge")),
            ).fetchone()
            if merging and not merging["retryable"]:
                raise Conflict("Resolve the previous merge outcome before revising")
            revision = candidate.revision + 1
            connection.execute(
                "INSERT INTO candidate_rounds VALUES(?,?,?,NULL,NULL,?)",
                (lease.change_id, revision, base, reason),
            )
            detail = {
                "expected_revision": revision,
                "base_commit": base,
                "previous_candidate": candidate.model_dump(mode="json"),
                "repair_reason": reason,
                "deadline": row["deadline"],
                "status": "waiting",
            }
            connection.execute(
                "UPDATE changes SET stage='preparing',status='waiting',revision=revision+1,"
                "worker_id=NULL,lease_until=NULL,blocker=NULL,result=?,updated_at=? WHERE id=?",
                (encode(detail), self.clock(), lease.change_id),
            )
            self._event(connection, lease.change_id, "preparing", detail)

        await self.write(revise)

    async def request_cancel(self, change_id: str, expected_revision: int) -> Json:
        def cancel(connection: sqlite3.Connection) -> Json:
            row = connection.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
            if row is None or row["revision"] != expected_revision:
                raise Conflict("Maintenance revision changed")
            if row["stage"] in TERMINAL:
                return dict(row)
            connection.execute(
                "UPDATE changes SET cancel_requested=1,status='queued',generation=generation+1,revision=revision+1,worker_id=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                (self.clock(), change_id),
            )
            self._event(connection, change_id, "cancel_requested", {})
            updated = connection.execute(
                "SELECT * FROM changes WHERE id=?", (change_id,)
            ).fetchone()
            assert updated is not None
            return dict(updated)

        return await self.write(cancel)

    async def events(self, after: int = 0, limit: int = 100) -> list[Json]:
        if after < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid event page")
        rows = await self.read(
            "SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit)
        )
        return [{**row, "detail": json.loads(row["detail"])} for row in rows]

    async def signal(self, change_id: str, name: str, body: Json) -> Json:
        if (
            name not in {"submission", "review", "round_jobs", "cancel", "rollback", "retry"}
            or len(encode(body)) > 32000
        ):
            raise Denied("Invalid controller signal")

        def signal(connection: sqlite3.Connection) -> Json:
            row = connection.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
            if not row:
                raise Denied("Unknown maintenance change")
            signal_name = name
            revision, current_revision = 0, 0
            if name in {"submission", "review", "round_jobs"}:
                revision = (
                    body.get("candidate", {}).get("revision")
                    if name == "review"
                    else body.get("revision")
                )
                if type(revision) is not int or revision < 1:
                    raise Denied("Signal requires a candidate revision")
                current = connection.execute(
                    "SELECT * FROM candidate_rounds WHERE change_id=? ORDER BY revision DESC LIMIT 1",
                    (change_id,),
                ).fetchone()
                if not current or revision > current["revision"]:
                    raise Conflict("Signal names an unissued candidate revision")
                current_revision = current["revision"]
                signal_name = round_effect(revision, name)
                if name == "round_jobs":
                    jobs = RoundJobs.model_validate(body)
                    if jobs.coding_job_id == jobs.review_job_id:
                        raise Denied("Coding and review jobs must be independent")
                    if revision == current_revision and current["coding_job_id"] is None:
                        if connection.execute(
                            "SELECT 1 FROM candidate_rounds WHERE coding_job_id IN (?,?) OR review_job_id IN (?,?)",
                            (jobs.coding_job_id, jobs.review_job_id) * 2,
                        ).fetchone():
                            raise Denied("Candidate jobs cannot be reused")
                        connection.execute(
                            "UPDATE candidate_rounds SET coding_job_id=?,review_job_id=? WHERE change_id=? AND revision=?",
                            (jobs.coding_job_id, jobs.review_job_id, change_id, revision),
                        )
            previous = connection.execute(
                "SELECT body FROM signals WHERE change_id=? AND name=?", (change_id, signal_name)
            ).fetchone()
            if previous:
                if name == "retry":
                    connection.execute(
                        "UPDATE changes SET status='queued' WHERE id=? AND status IN ('blocked','auth_wait','quota_wait','uncertain')",
                        (change_id,),
                    )
                    return {"accepted": True}
                if json.loads(previous["body"]) != body:
                    raise Conflict("Signal identity binds a different request")
                return {"accepted": True}
            if revision < current_revision:
                raise Conflict("A new signal cannot alter an earlier candidate revision")
            connection.execute(
                "INSERT INTO signals VALUES(?,?,?)", (change_id, signal_name, encode(body))
            )
            if name == "cancel":
                connection.execute("UPDATE changes SET cancel_requested=1 WHERE id=?", (change_id,))
            if name == "rollback":
                if row["stage"] != "deployed":
                    raise Denied("Explicit rollback requires a completed deployment")
                connection.execute(
                    "UPDATE changes SET stage='observing',cancel_requested=1,status='queued' WHERE id=?",
                    (change_id,),
                )
            elif row["status"] in ("waiting", "blocked", "auth_wait", "quota_wait") or (
                name == "retry" and row["status"] == "uncertain"
            ):
                connection.execute("UPDATE changes SET status='queued' WHERE id=?", (change_id,))
            return {"accepted": True}

        return await self.write(signal)

    async def get_signal(self, change_id: str, name: str) -> Json | None:
        row = await self.one(
            "SELECT body FROM signals WHERE change_id=? AND name=?", (change_id, name)
        )
        return json.loads(row["body"]) if row else None

    async def effect_intent(self, lease: ControllerLease, name: str, request: Json) -> Json:
        def reserve(connection: sqlite3.Connection) -> Json:
            self.check(connection, lease)
            row = connection.execute(
                "SELECT * FROM effects WHERE change_id=? AND name=?", (lease.change_id, name)
            ).fetchone()
            if row:
                if json.loads(row["request"]) != request:
                    raise Conflict("Effect identity already binds different content")
                if row["retryable"]:
                    connection.execute(
                        "UPDATE effects SET retryable=0 WHERE change_id=? AND name=?",
                        (lease.change_id, name),
                    )
                    return {"status": "new", "receipt": None}
                return dict(row)
            connection.execute(
                "INSERT INTO effects(change_id,name,request,status,receipt,updated_at) VALUES(?,?,?,'intent',NULL,?)",
                (lease.change_id, name, encode(request), self.clock()),
            )
            return {"status": "new", "receipt": None}

        return await self.write(reserve)

    async def effect_receipt(self, lease: ControllerLease, name: str, receipt: Json | None) -> None:
        def record(connection: sqlite3.Connection) -> None:
            self.check(connection, lease)
            connection.execute(
                "UPDATE effects SET status=?,receipt=?,updated_at=? WHERE change_id=? AND name=?",
                (
                    "succeeded" if receipt is not None else "uncertain",
                    encode(receipt) if receipt is not None else None,
                    self.clock(),
                    lease.change_id,
                    name,
                ),
            )

        await self.write(record)

    async def receipt(self, change_id: str, name: str) -> Json | None:
        row = await self.one(
            "SELECT receipt FROM effects WHERE change_id=? AND name=? AND status='succeeded'",
            (change_id, name),
        )
        return json.loads(row["receipt"]) if row else None

    async def wake_waiting(self) -> None:
        await self.execute("UPDATE changes SET status='queued' WHERE status='waiting'")

    async def no_effect(self, lease: ControllerLease, name: str) -> None:
        def record(connection: sqlite3.Connection) -> None:
            self.check(connection, lease)
            connection.execute(
                "UPDATE effects SET retryable=1 WHERE change_id=? AND name=?",
                (lease.change_id, name),
            )

        await self.write(record)
