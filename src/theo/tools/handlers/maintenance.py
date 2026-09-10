"""Private, run-authorized maintenance and operating-control tool adapters.

Standing authority comes from host-created context. These handlers cannot change
installation policy or convert reliability evidence into operating permission.
"""

from theo.domain import Json, ToolResult
from theo.operations.controls import Controls
from theo.tools.contracts import ToolCall
from theo.work.maintenance import Maintenance


async def runtime_control(call: ToolCall, args: Json) -> ToolResult:
    result = await Controls(call.db, call.settings).set(
        args["scope"],
        args["paused"],
        args["reason"],
        context=call.context,
        expected_revision=args["expected_revision"],
    )
    return ToolResult(status="committed", data=result)


async def begin(call: ToolCall, args: Json) -> ToolResult:
    return ToolResult(
        status="committed", data=await Maintenance(call.db, call.settings).begin(call.context, args)
    )


async def submit(call: ToolCall, args: Json) -> ToolResult:
    return ToolResult(
        status="committed",
        data=await Maintenance(call.db, call.settings).submit(call.context, args),
    )


async def review(call: ToolCall, args: Json) -> ToolResult:
    return ToolResult(
        status="committed",
        data=await Maintenance(call.db, call.settings).review(call.context, args),
    )


async def status(call: ToolCall, args: Json) -> ToolResult:
    return ToolResult(
        status="completed",
        data=await Maintenance(call.db, call.settings).status(call.context, args.get("change_id")),
    )


async def cancel(call: ToolCall, args: Json) -> ToolResult:
    return ToolResult(
        status="committed",
        data=await Maintenance(call.db, call.settings).control(call.context, args),
    )


async def rollback(call: ToolCall, args: Json) -> ToolResult:
    return ToolResult(
        status="committed",
        data=await Maintenance(call.db, call.settings).control(call.context, args, rollback=True),
    )
