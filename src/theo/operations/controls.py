"""Owner-authorized operational controls, distinct from reliability evidence.

The same decision service serves model tools, owner commands and admission. It
never changes account eligibility, qualification records or the standing policy.
"""

import sqlite3

from theo.config import Settings
from theo.domain import Conflict, ControlScope, Denied, Json, ToolContext, uid
from theo.operations.qualification import qualification_status
from theo.storage import Database
from theo.work.jobs import Jobs


class Controls:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings, self.owner = db, settings, settings.owner_id

    async def readiness(self) -> Json:
        """Operating permission does not attest to a completed production soak."""
        if self.settings.operating_mode == "qualified":
            evidence = await qualification_status(self.db, self.settings)
            missing = [key for key, passed in evidence["deployment_gates"].items() if not passed]
            return {"allowed": not missing, "mode": "qualified", "blockers": missing}
        missing: list[str] = []
        if not self.settings.isolation_verified or self.settings.worker_home is None:
            missing.append("verified_native_isolation")
        if not self.settings.primary_backend or not self.settings.primary_model:
            missing.append("configured_native_route")
        return {"allowed": not missing, "mode": "owner_authorized", "blockers": missing}

    async def snapshot(self) -> Json:
        rows = await self.db.read("SELECT key,value FROM control WHERE owner_id=?", (self.owner,))
        values = {row["key"]: row["value"] for row in rows}
        return {
            "revision": int(values.get("runtime_control_revision", "0")),
            "paused": {
                scope: values.get(scope + "_paused", "false") == "true"
                for scope in (
                    "background",
                    "autonomy",
                    "requested_work",
                    "models",
                    "deployments",
                    "notifications",
                )
            },
            "draining": values.get("maintenance_draining") == "true",
            "quarantined": values.get("quarantined") == "true",
        }

    async def set(
        self,
        scope: ControlScope,
        paused: bool,
        reason: str,
        *,
        conversation: str | None = None,
        context: ToolContext | None = None,
        expected_revision: int | None = None,
    ) -> Json:
        if not reason.strip() or len(reason) > 1000:
            raise ValueError("A bounded reason is required")
        if scope not in (
            "background",
            "autonomy",
            "requested_work",
            "models",
            "deployments",
            "notifications",
        ):
            raise ValueError("Unknown operating control")
        if context and (
            scope not in context.control_scopes or scope not in self.settings.model_runtime_controls
        ):
            raise Denied("Standing permission does not grant this runtime control to the worker")
        if not paused and scope in ("background", "autonomy", "requested_work"):
            readiness = await self.readiness()
            if not readiness["allowed"]:
                raise Denied("Operating prerequisites missing: " + ", ".join(readiness["blockers"]))
        conversation = context.conversation_id if context else conversation

        def commit(connection: sqlite3.Connection) -> Json:
            current = connection.execute(
                "SELECT value FROM control WHERE owner_id=? AND key='runtime_control_revision'",
                (self.owner,),
            ).fetchone()
            if (
                expected_revision is not None
                and (int(current[0]) if current else 0) != expected_revision
            ):
                raise Conflict("Runtime controls changed; inspect current status before retrying")
            if conversation:
                conv = connection.execute(
                    "SELECT c.id,d.private FROM conversations c LEFT JOIN telegram_destinations d ON d.conversation_id=c.id WHERE c.id=? AND c.owner_id=?",
                    (conversation, self.owner),
                ).fetchone()
                if conv is None or conv["private"] == 0:
                    raise Denied("Runtime controls require the private owner conversation")
            if context:
                job = Jobs(self.db, self.owner).check(
                    connection, context.job_id, context.generation
                )
                if job["origin"] != "requested" or job["conversation_id"] != conversation:
                    raise Denied("Only owner-requested work may change runtime controls")
            revision = self.db.set_control_in(
                connection, self.owner, scope + "_paused", "true" if paused else "false"
            )
            connection.execute(
                "INSERT INTO runtime_control_events VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    uid(),
                    self.owner,
                    conversation,
                    context.job_id if context else None,
                    scope,
                    int(paused),
                    reason,
                    revision,
                    self.db.clock(),
                ),
            )
            return {"scope": scope, "paused": paused, "revision": revision}

        return await self.db.write(commit)
