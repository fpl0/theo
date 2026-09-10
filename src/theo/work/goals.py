"""Persist structured goals and evidence-backed plan transitions.

Creates executable steps, owns promised progress checkpoints and requires outcome
evidence before completing work. Worker execution remains an application concern.
"""

import json
import math
import sqlite3

from theo.domain import Conflict, Denied, Json, encode, uid
from theo.storage import Database
from theo.work.jobs import Jobs


class Goals:
    def __init__(self, db: Database, owner: str):
        self.db, self.owner = db, owner

    async def inspect(self, goal_id: str) -> Json:
        def snapshot(db: sqlite3.Connection) -> Json:
            goal = db.execute(
                "SELECT * FROM goals WHERE id=? AND owner_id=?", (goal_id, self.owner)
            ).fetchone()
            if goal is None:
                raise Denied("Goal unavailable")
            return {
                **dict(goal),
                "steps": [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM plan_steps WHERE goal_id=? ORDER BY ordinal", (goal_id,)
                    )
                ],
                "checkpoints": [
                    dict(row)
                    for row in db.execute(
                        "SELECT id,body,due_at,status,evidence FROM commitments WHERE goal_id=? AND owner_id=? AND status='active' ORDER BY due_at",
                        (goal_id, self.owner),
                    )
                ],
            }

        return await self.db.write(snapshot)

    async def checkpoint(
        self,
        goal_id: str,
        next_update: float,
        *,
        parent_job: str,
        estimated_completion: float | None = None,
        estimate_basis: str | None = None,
    ) -> Json:
        """Persist a promised update and its future execution in one fenced transaction."""
        if not math.isfinite(next_update) or next_update <= self.db.clock():
            raise ValueError("Choose a future progress-update time")
        if estimated_completion is not None and (
            not math.isfinite(estimated_completion)
            or estimated_completion <= self.db.clock()
            or not estimate_basis
            or not estimate_basis.strip()
        ):
            raise ValueError("A completion estimate needs a future time and an evidence basis")

        def commit(db: sqlite3.Connection) -> Json:
            goal = db.execute(
                "SELECT * FROM goals WHERE id=? AND owner_id=?", (goal_id, self.owner)
            ).fetchone()
            if not goal or goal["status"] not in ("active", "blocked"):
                raise Denied("Progress updates require an active or blocked goal")
            self._close_checkpoints(db, goal_id, "superseded", except_job=parent_job)
            commitment = "goal-checkpoint:" + uid()
            job = Jobs(self.db, self.owner).insert(
                db,
                goal["conversation_id"],
                "goal_checkin",
                {
                    "goal_id": goal_id,
                    "commitment_id": commitment,
                    "text": "Deliver the promised progress update for goal "
                    + goal_id
                    + ". Inspect current goal, coverage, actual jobs and recent conversation. State completed stages, what remains, and a supported completion estimate or explain what still prevents one. If incomplete, persist the next checkpoint before giving its time, and ensure the next executable step owns continued work. This promised update must not require the owner to ask again. Do not repeat file-by-file logs or internal instructions. If complete, give the useful final result.",
                },
                commitment,
                parent=parent_job,
                available=next_update,
                deadline=next_update + 3600,
            )
            evidence = {
                "job_id": job,
                "estimated_completion_at": estimated_completion,
                "estimate_basis": estimate_basis,
            }
            db.execute(
                "INSERT INTO commitments VALUES(?,?,?,?,?,?,?)",
                (
                    commitment,
                    self.owner,
                    goal_id,
                    "Progress update: " + goal["title"],
                    next_update,
                    "active",
                    encode(evidence),
                ),
            )
            return {"id": commitment, "job_id": job, "next_update_at": next_update, **evidence}

        return await self.db.write(commit)

    def _close_checkpoints(
        self, db: sqlite3.Connection, goal_id: str, status: str, *, except_job: str | None = None
    ) -> None:
        for row in db.execute(
            "SELECT id,evidence FROM commitments WHERE owner_id=? AND goal_id=? AND status='active' AND id LIKE 'goal-checkpoint:%'",
            (self.owner, goal_id),
        ).fetchall():
            job = json.loads(row["evidence"])["job_id"]
            if job != except_job:
                Jobs(self.db, self.owner).cancel_in(db, job)
            db.execute("UPDATE commitments SET status=? WHERE id=?", (status, row["id"]))

    async def revise_step(self, step_id: str, expected: str, next_action: str) -> None:
        """Replace a stale next action without discarding progress or dependencies."""
        if not next_action.strip():
            raise ValueError("A plan step needs an executable next action")

        def revise(db: sqlite3.Connection) -> None:
            step = db.execute(
                "SELECT s.*,g.status goal_status FROM plan_steps s JOIN goals g ON g.id=s.goal_id WHERE s.id=? AND s.owner_id=?",
                (step_id, self.owner),
            ).fetchone()
            if step is None:
                raise Denied("Step unavailable")
            if step["next_action"] != expected:
                raise Conflict("Plan changed; inspect the current goal before revising it")
            if step["status"] == "completed" or step["goal_status"] in (
                "completed",
                "abandoned",
                "paused",
            ):
                raise Conflict("Do not revise completed or inactive work")
            if expected == next_action:
                return
            db.execute("UPDATE plan_steps SET next_action=? WHERE id=?", (next_action, step_id))
            db.execute(
                "UPDATE goals SET updated_at=? WHERE id=?", (self.db.clock(), step["goal_id"])
            )

        await self.db.write(revise)

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
        current_job: str | None = None,
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
            if status in ("completed", "abandoned", "paused"):
                self._close_checkpoints(
                    db,
                    goal_id,
                    "goal_completed" if status == "completed" else "cancelled",
                    except_job=current_job,
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
