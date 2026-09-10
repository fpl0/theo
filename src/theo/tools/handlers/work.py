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
    job = await db.one("SELECT origin FROM jobs WHERE id=? AND owner_id=?", (ctx.job_id, owner))
    assert job is not None
    schedule_id = await Scheduler(db, owner).create(
        ctx.conversation_id,
        args["text"],
        due=args.get("due_at"),
        cron=args.get("cron"),
        interval=args.get("interval_seconds"),
        timezone=args.get("timezone") or call.settings.timezone,
        mode=args["mode"],
        origin=job["origin"],
    )
    scheduled = await db.one(
        "SELECT id,kind,mode,origin,next_due,timezone FROM schedules WHERE id=? AND owner_id=?",
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


async def get_status(call: ToolCall, args: Json) -> ToolResult:
    data = await Jobs(call.db, call.context.owner_id).inspect(
        scope=call.scope,
        exclude_job_id=call.context.job_id,
        limit=args["limit"],
        offset=args["offset"],
    )
    if not call.scope:
        data["host_access"] = {
            "enabled": call.settings.host_access_enabled,
            "command_policy": call.settings.host_command_policy,
            "privileged_launcher_installed": call.settings.host_root_launcher is not None,
            "read_roots": [str(path) for path in call.settings.host_read_roots],
            "workspace_commands": "command_run is confined to the job workspace",
            "general_commands": (
                "host_command has standing owner permission, including as_root; do not ask again"
                if call.settings.host_command_policy == "standing"
                else "host_command requires exact approval except fixed diagnostics"
            ),
        }
        data["maintenance"] = {
            "configured": bool(
                call.settings.maintenance_socket
                and call.settings.maintenance_token_file
                and call.settings.maintenance_installation_id
            ),
            "proactive_permitted_by_core": call.settings.maintenance_proactive,
            "status_tool": "maintenance_status",
        }
    data["runtime_control_scopes"] = sorted(call.context.control_scopes) if not call.scope else []
    data["runtime_control_revision"] = int(
        next(
            (row["value"] for row in data["controls"] if row["key"] == "runtime_control_revision"),
            "0",
        )
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


async def goal_inspect(call: ToolCall, args: Json) -> ToolResult:
    return ToolResult(
        status="ok", data=await Goals(call.db, call.context.owner_id).inspect(args["id"])
    )


async def step_update(call: ToolCall, args: Json) -> ToolResult:
    await Goals(call.db, call.context.owner_id).revise_step(
        args["id"], args["expected_next_action"], args["next_action"]
    )
    return ToolResult(status="committed")


async def step_complete(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    owner = call.context.owner_id
    await Goals(db, owner).complete_step(args["id"], args["evidence"])
    return ToolResult(status="committed")
