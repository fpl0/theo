"""Plans advance only through attributed, observable evidence."""

import sqlite3

from theo.domain import Conflict, Denied, Json, encode, uid
from theo.storage import Database


class Goals:
    def __init__(self, db: Database, owner: str):
        self.db, self.owner = db, owner

    async def create(self, title: str, criteria: str, conversation: str, steps: list[Json]) -> str:
        if not title.strip() or not criteria.strip():
            raise ValueError("Goal needs a title and explicit outcome criteria")
        goal_id = uid()

        def insert(db: sqlite3.Connection) -> str:
            if not db.execute(
                "SELECT 1 FROM conversations WHERE id=? AND owner_id=?", (conversation, self.owner)
            ).fetchone():
                raise Denied("Conversation unavailable")
            db.execute(
                "INSERT INTO goals(id,owner_id,conversation_id,title,criteria,status,blocker,evidence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    goal_id,
                    self.owner,
                    conversation,
                    title,
                    criteria,
                    "active" if steps else "proposed",
                    None,
                    None,
                    self.db.clock(),
                    self.db.clock(),
                ),
            )
            for ordinal, step in enumerate(steps):
                if not step.get("title") or not step.get("next_action"):
                    raise ValueError("Every step needs a title and next action")
                db.execute(
                    "INSERT INTO plan_steps(id,owner_id,goal_id,ordinal,title,next_action,capabilities,status,evidence) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        uid(),
                        self.owner,
                        goal_id,
                        ordinal,
                        step["title"],
                        step["next_action"],
                        encode(step.get("capabilities", [])),
                        "pending",
                        None,
                    ),
                )
            return goal_id

        return await self.db.write(insert)

    async def update(
        self,
        goal_id: str,
        status: str,
        *,
        evidence: str | None = None,
        blocker: str | None = None,
        owner_override: bool = False,
    ) -> None:
        if status not in ("proposed", "active", "blocked", "paused", "completed", "abandoned"):
            raise ValueError("Unknown goal status")
        if status == "blocked" and not blocker:
            raise ValueError("Blocked goal requires a concrete missing condition")

        def update(db: sqlite3.Connection) -> None:
            if not db.execute(
                "SELECT 1 FROM goals WHERE id=? AND owner_id=?", (goal_id, self.owner)
            ).fetchone():
                raise Denied("Goal unavailable")
            counts = db.execute(
                "SELECT count(*),sum(status<>'completed') FROM plan_steps WHERE goal_id=?",
                (goal_id,),
            ).fetchone()
            assert counts
            if status == "completed" and (
                not evidence or (not owner_override and (not counts[0] or counts[1]))
            ):
                raise Conflict("Completion requires evidence and completed executable steps")
            if status == "active" and not counts[0]:
                raise Conflict("Goal requires an executable plan")
            db.execute(
                "UPDATE goals SET status=?,evidence=?,blocker=?,updated_at=? WHERE id=?",
                (status, evidence, blocker, self.db.clock(), goal_id),
            )

        await self.db.write(update)

    async def complete_step(self, step_id: str, evidence: str) -> None:
        if not evidence.strip():
            raise ValueError("Step completion requires evidence")

        def complete(db: sqlite3.Connection) -> None:
            step = db.execute(
                "SELECT * FROM plan_steps WHERE id=? AND owner_id=?", (step_id, self.owner)
            ).fetchone()
            if step is None:
                raise Denied("Step unavailable")
            if db.execute(
                "SELECT 1 FROM step_dependencies d JOIN plan_steps s ON s.id=d.depends_on WHERE d.step_id=? AND s.status<>'completed'",
                (step_id,),
            ).fetchone():
                raise Conflict("Dependencies remain incomplete")
            db.execute(
                "UPDATE plan_steps SET status='completed',evidence=? WHERE id=?",
                (evidence, step_id),
            )
            db.execute(
                "UPDATE goals SET updated_at=? WHERE id=?", (self.db.clock(), step["goal_id"])
            )

        await self.db.write(complete)
