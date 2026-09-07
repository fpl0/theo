"""Reviewed skill versions and observable evaluation evidence; grants never self-expand."""

import json
import sqlite3

from theo.domain import Conflict, Denied, Json, encode, uid
from theo.storage import Database


class Improvement:
    def __init__(self, db: Database, owner: str):
        self.db, self.owner = db, owner

    async def propose_skill(self, name: str, body: str, triggers: list[str], source: str) -> str:
        if not name or not body.strip() or not triggers or not source:
            raise ValueError("Skills require source evidence, body and narrow triggers")
        skill_id = uid()

        def propose(connection: sqlite3.Connection) -> str:
            row = connection.execute(
                "SELECT COALESCE(max(version),0)+1 FROM skills WHERE owner_id=? AND name=?",
                (self.owner, name),
            ).fetchone()
            assert row
            connection.execute(
                "INSERT INTO skills(id,owner_id,name,version,body,triggers,grants,status,evaluation,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    skill_id,
                    self.owner,
                    name,
                    row[0],
                    body,
                    encode(triggers),
                    "[]",
                    "proposed",
                    None,
                    source,
                    self.db.clock(),
                ),
            )
            return skill_id

        return await self.db.write(propose)

    async def evaluate_skill(self, skill_id: str, cases: list[Json]) -> Json:
        if len(cases) < 3 or any(
            set(case) != {"input", "expected", "observed", "passed"} for case in cases
        ):
            raise ValueError("Provide at least three recorded input/expected/observed cases")
        passed = all(case["passed"] is True and case["observed"] for case in cases)
        result = {
            "passed": passed,
            "cases": cases,
            "method": "operator_recorded_observable_results",
        }
        changed = await self.db.execute(
            "UPDATE skills SET status=?,evaluation=? WHERE id=? AND owner_id=? AND status IN ('proposed','tested')",
            ("tested" if passed else "proposed", encode(result), skill_id, self.owner),
        )
        if not changed:
            raise Conflict("Skill unavailable or already active")
        return result

    async def activate_skill(self, skill_id: str) -> None:
        def activate(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT * FROM skills WHERE id=? AND owner_id=? AND status='tested'",
                (skill_id, self.owner),
            ).fetchone()
            if row is None or not json.loads(row["evaluation"])["passed"]:
                raise Denied("Skill activation requires recorded passing evaluation")
            connection.execute(
                "UPDATE skills SET status='retired' WHERE owner_id=? AND name=? AND status='active'",
                (self.owner, row["name"]),
            )
            connection.execute("UPDATE skills SET status='active' WHERE id=?", (skill_id,))

        await self.db.write(activate)

    async def rollback_skill(self, skill_id: str) -> None:
        await self.db.execute(
            "UPDATE skills SET status='retired' WHERE id=? AND owner_id=? AND status='active'",
            (skill_id, self.owner),
        )


class Critic:
    def __init__(self, db: Database, owner: str):
        self.db, self.owner = db, owner

    async def queue(self) -> int:
        from theo.jobs import Jobs

        rows = await self.db.read(
            "SELECT a.* FROM actions a WHERE a.owner_id=? AND a.status='ready' AND a.critic_status='unchecked' AND EXISTS(SELECT 1 FROM outbox o WHERE o.action_id=a.id AND o.discretionary=1)",
            (self.owner,),
        )
        queued = 0
        for row in rows:
            prompt = (
                'Review this optional outreach. Reply ONLY with JSON {"verdict":"pass" or "block","reason":"..."}. Check evidence, relevance, tone and truthful completion. You have read-only tools and no action authority.\n'
                + row["request"]
            )
            await Jobs(self.db, self.owner).enqueue(
                row["conversation_id"],
                "critic",
                {"text": prompt, "action_id": row["id"], "request_hash": row["request_hash"]},
                "critic:" + row["id"],
                deadline=self.db.clock() + 120,
            )
            queued += 1
        return queued

    async def record(self, action_id: str, request_hash: str, output: str) -> str:
        try:
            result = json.loads(output)
            if (
                set(result) != {"verdict", "reason"}
                or result["verdict"] not in ("pass", "block")
                or not result["reason"]
            ):
                raise ValueError("Invalid critic verdict")
            verdict = "passed" if result["verdict"] == "pass" else "blocked"
        except ValueError, TypeError:
            verdict = "unchecked"
        await self.db.execute(
            "UPDATE actions SET critic_status=? WHERE id=? AND owner_id=? AND request_hash=? AND status='ready'",
            (verdict, action_id, self.owner, request_hash),
        )
        if verdict == "passed":
            await self.db.execute(
                "UPDATE outbox SET available_at=?,error=NULL WHERE action_id=? AND status='ready' AND EXISTS(SELECT 1 FROM actions WHERE id=? AND owner_id=? AND request_hash=? AND critic_status='passed')",
                (self.db.clock(), action_id, action_id, self.owner, request_hash),
            )
        return verdict
