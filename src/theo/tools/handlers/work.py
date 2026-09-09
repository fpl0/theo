"""Model-facing schedules, delegated jobs and goal-plan transitions.

Persists work before returning a commitment and includes local due-time evidence
for newly created schedules. Execution belongs to the work services.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from theo.domain import (
    Json,
    ToolResult,
    digest,
)
from theo.tools.contracts import ToolCall
from theo.work.goals import Goals
from theo.work.jobs import Jobs
from theo.work.scheduling import Scheduler


async def schedule_task(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    owner = call.context.owner_id
    schedule_id = await Scheduler(db, owner).create(
        ctx.conversation_id,
        args["text"],
        due=args.get("due_at"),
        cron=args.get("cron"),
        interval=args.get("interval_seconds"),
        timezone=args["timezone"],
    )
    scheduled = await db.one(
        "SELECT id,kind,next_due,timezone FROM schedules WHERE id=? AND owner_id=?",
        (schedule_id, owner),
    )
    assert scheduled is not None
    data = {
        **scheduled,
        "next_due_local": datetime.fromtimestamp(
            scheduled["next_due"], ZoneInfo(scheduled["timezone"])
        ).isoformat(),
    }
    return ToolResult(status="committed", data=data)


async def list_tasks(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    owner = call.context.owner_id
    scope = call.scope
    data = await db.read(
        "SELECT * FROM schedules WHERE owner_id=? AND (? IS NULL OR conversation_id=?) ORDER BY next_due",
        (owner, scope, scope),
    )
    return ToolResult(status="ok", data=data)


async def delete_task(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    owner = call.context.owner_id
    await Scheduler(db, owner).cancel(args["id"])
    return ToolResult(status="committed")


async def delegate(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    owner = call.context.owner_id
    data = {
        "job_id": await Jobs(db, owner).enqueue(
            ctx.conversation_id,
            "delegated",
            {"text": args["task"]},
            f"delegate:{ctx.job_id}:{digest(args)}",
            parent=ctx.job_id,
            deadline=db.clock() + args["deadline_seconds"],
        )
    }
    return ToolResult(status="committed", data=data)


async def goal_create(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    owner = call.context.owner_id
    data = {
        "id": await Goals(db, owner).create(
            args["title"], args["criteria"], ctx.conversation_id, args["steps"]
        )
    }
    return ToolResult(status="committed", data=data)


async def goal_update(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    owner = call.context.owner_id
    await Goals(db, owner).update(
        args["id"],
        args["status"],
        evidence=args.get("evidence"),
        blocker=args.get("blocker"),
    )
    return ToolResult(status="committed")


async def step_complete(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    owner = call.context.owner_id
    await Goals(db, owner).complete_step(args["id"], args["evidence"])
    return ToolResult(status="committed")
