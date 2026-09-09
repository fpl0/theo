"""Model-facing usage reports, work ratings and skill proposals.

Combines subjective ratings with recorded artifact and delivery evidence.
Proposed skills remain inactive until the owner reviews them.
"""

from theo.backends.policy import Accounts
from theo.domain import (
    Json,
    ToolResult,
    encode,
    uid,
)
from theo.tools.contracts import ToolCall


async def get_cost_report(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    owner = call.context.owner_id
    data = await Accounts(db, owner).usage()
    return ToolResult(status="ok", data=data)


async def log_deep_work_quality(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    owner = call.context.owner_id
    artifacts = await db.one(
        "SELECT count(*) n FROM artifacts WHERE owner_id=? AND run_id=? AND validated=1",
        (owner, ctx.run_id),
    )
    delivered = await db.one(
        "SELECT count(*) n FROM actions WHERE owner_id=? AND run_id=? AND status='succeeded'",
        (owner, ctx.run_id),
    )
    data = {
        "rating": args["rating"],
        "rationale": args["rationale"],
        "artifact_count": artifacts["n"] if artifacts else 0,
        "delivered_count": delivered["n"] if delivered else 0,
    }
    await db.execute(
        "INSERT INTO feedback VALUES(?,?,?,?,?,?,?,?)",
        (uid(), owner, ctx.run_id, None, "quality", encode(data), 0, db.clock()),
    )
    return ToolResult(status="committed", data=data)


async def skill_propose(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    owner = call.context.owner_id
    source = f"run:{ctx.run_id}"
    from theo.work.improvement import Improvement

    skill = await Improvement(db, owner).propose_skill(
        args["name"], args["body"], args["triggers"], source
    )
    return ToolResult(status="pending_review", data={"skill_id": skill})
