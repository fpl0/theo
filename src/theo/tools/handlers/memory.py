"""Model-facing memory, provenance, attention and fact-proposal operations.

Uses the invocation conversation scope for retrieval and resource labeling.
Corrections, fact changes and bulk inputs retain their existing review checks.
"""

from theo.domain import (
    Denied,
    Json,
    ToolResult,
    digest,
    encode,
    uid,
)
from theo.memory.store import Memory
from theo.privacy import label_in
from theo.tools.authorization import authorize
from theo.tools.contracts import ToolCall


async def remember(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    owner = call.context.owner_id
    scope = call.scope
    source = f"run:{ctx.run_id}"
    memory = Memory(db, owner, scope)
    source_message = args.get("source_message_id")
    if source_message:
        row = await db.one(
            "SELECT content FROM messages WHERE id=? AND owner_id=? AND conversation_id=? AND role='user'",
            (source_message, owner, ctx.conversation_id),
        )
        if row is None:
            raise Denied("Source must be an actual owner message in this conversation")
        source = f"message:{source_message}"
    if args.get("target_memory_id"):
        if args.get("expected_revision") is None:
            raise ValueError("Correction requires expected_revision")
        data = {
            "correction_id": await memory.propose(
                args["target_memory_id"], args["expected_revision"], args["body"], source
            )
        }
        return ToolResult(status="pending_review", data=data)
    data = {"id": await memory.remember(args["body"], kind=args["kind"], source=source)}
    return ToolResult(status="committed", data=data)


async def recall(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    owner = call.context.owner_id
    scope = call.scope
    memory = Memory(db, owner, scope)
    data = await memory.search(args["query"], args["limit"])
    return ToolResult(status="ok", data=data)


async def forget(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    owner = call.context.owner_id
    scope = call.scope
    memory = Memory(db, owner, scope)
    await memory.archive(args["id"])
    return ToolResult(status="committed")


async def restore(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    owner = call.context.owner_id
    scope = call.scope
    memory = Memory(db, owner, scope)
    data = {"revision": await memory.restore(args["id"], args.get("revision"))}
    return ToolResult(status="committed", data=data)


async def memory_history(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    owner = call.context.owner_id
    scope = call.scope
    memory = Memory(db, owner, scope)
    data = await memory.history(args["id"])
    return ToolResult(status="ok", data=data)


async def review_corrections(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    owner = call.context.owner_id
    data = await db.read(
        "SELECT * FROM corrections WHERE owner_id=? AND status='proposed'", (owner,)
    )
    return ToolResult(status="ok", data=data)


async def connect(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    owner = call.context.owner_id
    scope = call.scope
    source = f"run:{ctx.run_id}"
    memory = Memory(db, owner, scope)
    data = {
        "id": await memory.connect(args["source_id"], args["target_id"], args["relation"], source)
    }
    return ToolResult(status="committed", data=data)


async def bulk_memory(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    data: list[Json] = []
    for item in args["memories"]:
        await authorize(db, ctx, "remember", item)
        data.append((await remember(call, item)).model_dump(mode="json"))
    return ToolResult(status="committed", data=data)


async def recall_conversation(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    owner = call.context.owner_id
    data = await db.read(
        "SELECT id,role,content,parts,created_at FROM messages WHERE owner_id=? AND conversation_id=? ORDER BY sequence DESC LIMIT ?",
        (owner, ctx.conversation_id, args["limit"]),
    )
    return ToolResult(status="ok", data=data)


async def pin_attention(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    owner = call.context.owner_id
    scope = call.scope
    source = f"run:{ctx.run_id}"
    data = {"id": uid()}
    await db.execute(
        "INSERT INTO attention_pins VALUES(?,?,?,?,?,?)",
        (data["id"], owner, args["body"], source, args.get("expires_at"), db.clock()),
    )
    await db.write(lambda connection: label_in(connection, "pin", data["id"], scope))
    return ToolResult(status="committed", data=data)


async def unpin_attention(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    owner = call.context.owner_id
    await db.execute("DELETE FROM attention_pins WHERE id=? AND owner_id=?", (args["id"], owner))
    return ToolResult(status="committed")


async def fact_propose(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    owner = call.context.owner_id
    row = await db.one(
        "SELECT id FROM messages WHERE id=? AND owner_id=? AND role='user'",
        (args["source_message_id"], owner),
    )
    if row is None:
        raise Denied("Fact proposal requires an owner source")
    proposal = uid()
    await db.execute(
        "INSERT OR IGNORE INTO proposals VALUES(?,?,?,?,?,?,?,?,?)",
        (
            proposal,
            owner,
            "fact",
            digest(args),
            args["source_message_id"],
            encode(args),
            "proposed",
            None,
            db.clock(),
        ),
    )
    return ToolResult(status="pending_review", data={"proposal_id": proposal})
