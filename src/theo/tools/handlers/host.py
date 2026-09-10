"""Private-owner scoped reads and host commands with durable action approvals."""

import asyncio
from pathlib import Path

from theo.delivery.ledger import Delivery
from theo.domain import Denied, Json, ToolResult, digest
from theo.execution.host import needs_approval
from theo.execution.host_files import read_host_path
from theo.tools.contracts import ToolCall


async def read(call: ToolCall, args: Json) -> ToolResult:
    if not call.settings.host_access_enabled or call.scope is not None:
        raise Denied("Host reads require enabled private-owner permission")
    if not call.settings.host_read_roots:
        raise Denied("No host_read_roots configured; the operator must select readable paths once")
    standing = call.settings.host_command_policy == "standing"
    protected = [call.db.root, Path.home() / ".codex", Path.home() / ".claude"]
    if call.settings.worker_home:
        protected.extend(
            [call.settings.worker_home / ".codex", call.settings.worker_home / ".claude"]
        )
    result = await asyncio.to_thread(
        read_host_path,
        Path(args["path"]),
        call.settings.host_read_roots,
        () if standing else tuple(protected),
        offset=args["offset"],
        limit=args["limit"],
        protect_credentials=not standing,
    )
    # Do not return private data after the worker has lost its lease.
    await call.db.write(lambda connection: None)
    return ToolResult(status="ok", data=result)


async def command(call: ToolCall, args: Json) -> ToolResult:
    if not call.settings.host_access_enabled or call.scope is not None:
        raise Denied("Host access requires enabled private-owner standing permission")
    required = needs_approval(args, standing=call.settings.host_command_policy == "standing")
    action = await Delivery(call.db, call.settings).prepare(
        call.context.conversation_id,
        "host_command",
        args,
        "host:" + call.context.job_id + ":" + digest(args),
        job_id=call.context.job_id,
        run_id=call.context.run_id,
        generation=call.context.generation,
        role="progress",
        require_approval=required,
        durable_obligation=True,
        expires_at=call.db.clock() + 3600,
    )
    row = await call.db.one("SELECT status FROM actions WHERE id=?", (action,))
    approval = await call.db.one("SELECT id FROM approvals WHERE action_id=?", (action,))
    return ToolResult(
        status=row["status"] if row else "failed",
        action_id=action,
        data={
            "approval_required": required,
            "approval_id": approval["id"] if approval else None,
            "next_step": "Owner reviews the exact command in /review"
            if required
            else "Read action_status for the observed command result",
        },
    )
