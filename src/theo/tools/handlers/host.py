"""Private-owner host access through exact, durable action approvals."""

from theo.delivery.ledger import Delivery
from theo.domain import Denied, Json, ToolResult, digest
from theo.execution.host import needs_approval
from theo.tools.contracts import ToolCall


async def command(call: ToolCall, args: Json) -> ToolResult:
    if not call.settings.host_access_enabled or call.scope is not None:
        raise Denied("Host access requires enabled private-owner standing permission")
    required = needs_approval(args)
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
