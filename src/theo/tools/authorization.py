"""Enforce run leases and conversation visibility before tool execution.

BoundDatabase rechecks the lease inside every write transaction. Authorization
also restricts group resources and correction jobs with previously recorded effects.
"""

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from theo.domain import (
    Denied,
    Json,
    ToolContext,
)
from theo.privacy import group_scope, require_resource
from theo.storage import Database, Transaction
from theo.work.jobs import Jobs


class BoundDatabase(Database):
    """Every mutation rechecks the lease inside the same SQLite transaction."""

    def __init__(self, parent: Database, context: ToolContext):
        self.parent, self.context = parent, context
        self.root, self.path, self.clock = parent.root, parent.path, parent.clock

    async def write[T](self, fn: Transaction[T]) -> T:
        def guarded(db: sqlite3.Connection) -> T:
            Jobs(self.parent, self.context.owner_id).check(
                db, self.context.job_id, self.context.generation
            )
            return fn(db)

        return await self.parent.write(guarded)

    async def read(self, sql: str, args: Sequence[Any] = ()) -> list[Json]:
        await self.write(lambda db: None)
        return await self.parent.read(sql, args)


async def authorize(db: Database, ctx: ToolContext, name: str, args: Json) -> str | None:
    scope = await group_scope(db, ctx.conversation_id)
    if scope:
        if name in {"get_cost_report", "command_run", "skill_propose"}:
            raise Denied("Use the owner private chat for this capability")
        for argument, kind in (("artifact_id", "artifact"), ("source_message_id", "message")):
            if argument in args:
                if kind == "message":
                    if not await db.one(
                        "SELECT 1 FROM messages WHERE id=? AND conversation_id=?",
                        (args[argument], scope),
                    ):
                        raise Denied("Source is not in this conversation")
                else:
                    await require_resource(db, kind, args[argument], scope)
        if name in ("action_status", "delete_task", "goal_update", "step_complete"):
            table = {
                "action_status": "actions",
                "delete_task": "schedules",
                "goal_update": "goals",
                "step_complete": "plan_steps",
            }[name]
            sql = (
                f"SELECT 1 FROM {table} WHERE id=? AND conversation_id=?"
                if table != "plan_steps"
                else "SELECT 1 FROM plan_steps s JOIN goals g ON g.id=s.goal_id WHERE s.id=? AND g.conversation_id=?"
            )
            if not await db.one(sql, (args["id"], scope)):
                raise Denied("Object is not in this conversation")
        if name == "unpin_attention":
            await require_resource(db, "pin", args["id"], scope)
        if name in (
            "forward",
            "edit_message",
            "delete_message",
            "pin",
            "react",
            "get_reactions",
        ):
            if not await db.one(
                "SELECT 1 FROM telegram_messages WHERE conversation_id=? AND message_id=?",
                (scope, args["message_id"]),
            ):
                raise Denied("Message is not in this conversation")
            if args.get("target") or args.get("destination_id") or name == "forward":
                raise Denied("Review cross-conversation message operations privately")
    correction = await db.one("SELECT payload FROM jobs WHERE id=?", (ctx.job_id,))
    if (
        correction
        and json.loads(correction["payload"]).get("correction_effects")
        and name
        not in {
            "recall",
            "recall_conversation",
            "memory_history",
            "review_corrections",
            "list_tasks",
            "action_status",
            "file_read",
            "get_reactions",
            "get_cost_report",
            "send_message",
            "reply",
        }
    ):
        raise Denied(
            "This correction has prior effects; a fresh owner request is required before creating new effects"
        )
    return scope
