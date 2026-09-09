"""Model-facing workspace files, artifacts, public browsing and local media.

Applies bounded path and content checks before calling the content and execution
services; all invocations have already passed broker authorization.
"""

import asyncio

from theo.content.artifacts import Artifacts, scoped_path
from theo.content.web import browse as browse_public
from theo.content.web import render_public_page
from theo.domain import (
    Json,
    ToolResult,
    uid,
)
from theo.tools.contracts import ToolCall


async def browse(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    data = await browse_public(args["url"])
    if args["screenshot"]:
        raw = await render_public_page(args["url"])
        data["screenshot"] = await Artifacts(db, call.settings).store(
            raw, "page.png", args["url"], ctx.run_id
        )
    return ToolResult(status="ok", data=data)


async def artifact_register(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    data = await Artifacts(db, call.settings).register(
        ctx.workspace, args["path"], args["description"], ctx.run_id
    )
    return ToolResult(status="committed", data=data)


async def file_read(call: ToolCall, args: Json) -> ToolResult:
    ctx = call.context
    path = scoped_path(ctx.workspace, args["path"])
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("File exceeds text read limit")
    data = {"path": args["path"], "content": await asyncio.to_thread(path.read_text)}
    return ToolResult(status="ok", data=data)


async def file_write(call: ToolCall, args: Json) -> ToolResult:
    ctx = call.context
    path = scoped_path(ctx.workspace, args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_text, args["content"])
    data = {"path": args["path"], "bytes": len(args["content"].encode())}
    return ToolResult(status="committed", data=data)


async def command_run(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    from theo.execution.workspaces import execute_scoped

    data = await execute_scoped(
        call.settings, db.root, ctx.workspace, args["argv"], args["timeout_seconds"]
    )
    if data["exit_code"] != 0:
        return ToolResult(status="failed", data=data, error="Command exited unsuccessfully")
    return ToolResult(status="committed", data=data)


async def voice_create(call: ToolCall, args: Json) -> ToolResult:
    db = call.db
    ctx = call.context
    from theo.content.media import speak

    path = ctx.workspace / ("voice-" + uid() + ".ogg")
    await speak(args["text"], path, args.get("voice"))
    data = await Artifacts(db, call.settings).register(
        ctx.workspace, path.name, "Local voice response", ctx.run_id
    )
    return ToolResult(status="committed", data=data)
