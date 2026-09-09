"""Queue outbound effects and inspect delivery or reaction evidence.

Validates destinations and artifacts, then delegates intent and approval state
to Delivery. Handlers never call Telegram or claim a queued action was delivered.
"""

from theo.content.artifacts import Artifacts
from theo.delivery.ledger import Delivery
from theo.domain import (
    Denied,
    Json,
    ToolResult,
    digest,
)
from theo.privacy import require_resource
from theo.tools.contracts import ToolCall


async def send(call: ToolCall, args: Json, *, operation: str) -> ToolResult:
    db = call.db
    ctx = call.context
    owner = call.context.owner_id
    scope = call.scope
    name = operation
    if args.get("target") and args.get("destination_id"):
        raise ValueError("Choose either target or destination_id")
    if name == "send_media_group":
        kinds = {item["kind"] for item in args["items"]}
        if ("audio" in kinds or "document" in kinds) and len(kinds) > 1:
            raise ValueError("Audio and document albums must contain a single media kind")
    for item in args.get("items", []):
        await require_resource(db, "artifact", item["artifact_id"], scope)
        await Artifacts(db, call.settings).content(item["artifact_id"])
    if "artifact_id" in args:
        await Artifacts(db, call.settings).content(args["artifact_id"])
    if name == "send_buttons":
        from theo.content.web import validate_url

        for button in args["buttons"]:
            validate_url(button["url"])
    role = args.pop("role", "progress")
    destination_id = args.pop("destination_id", None)
    target = args.pop("target", None)
    identity = {**args, "target": target, "destination_id": destination_id}
    key = (
        f"final:{ctx.job_id}" if role == "final" else f"tool:{ctx.job_id}:{name}:{digest(identity)}"
    )
    job = await db.one("SELECT lane,kind FROM jobs WHERE id=? AND owner_id=?", (ctx.job_id, owner))
    action = await Delivery(db, call.settings).prepare(
        ctx.conversation_id,
        name,
        args,
        key,
        job_id=ctx.job_id,
        run_id=ctx.run_id,
        generation=ctx.generation,
        role=role,
        autonomous=bool(job and job["lane"] == "background"),
        discretionary=bool(
            job
            and job["lane"] == "background"
            and not (role == "final" and job["kind"] in ("delegated", "deep_work"))
        ),
        target=target,
        destination_id=destination_id,
        require_approval=name in ("delete_message", "forward"),
    )
    row = await db.one("SELECT status FROM actions WHERE id=?", (action,))
    return ToolResult(status=str(row["status"]) if row else "failed", action_id=action)


async def get_reactions(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    owner = call.context.owner_id
    feedback_conversation = ctx.conversation_id
    if args.get("target") or args.get("destination_id"):
        binding = await db.one(
            "SELECT d.conversation_id FROM telegram_destinations d JOIN conversations c ON c.id=d.conversation_id WHERE d.owner_id=? AND (d.id=? OR c.target=?)",
            (owner, args.get("destination_id"), args.get("target")),
        )
        if not binding:
            raise Denied("Destination unavailable")
        feedback_conversation = binding["conversation_id"]
    data = await db.read(
        "SELECT body,created_at FROM feedback WHERE owner_id=? AND kind IN ('reaction','reaction_count') AND json_extract(body,'$.message_id')=? AND (? IS NULL OR json_extract(body,'$.conversation_id')=?)",
        (owner, args["message_id"], feedback_conversation, feedback_conversation),
    )
    return ToolResult(status="ok", data={"observed": data, "complete": False})


async def action_status(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    owner = call.context.owner_id
    data = await db.one(
        "SELECT id,operation,status,receipt,error FROM actions WHERE id=? AND owner_id=?",
        (args["id"], owner),
    )
    if data is None:
        raise Denied("Action unavailable")
    return ToolResult(status="ok", data=data)
