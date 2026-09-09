import pytest

from theo.application.coordinator import Coordinator
from theo.backends.base import NativeBackend
from theo.delivery.ledger import Delivery
from theo.domain import ExecutionOutcome, Outcome
from theo.memory.context import ContextAssembler
from theo.memory.store import Memory
from theo.tools.broker import ToolBroker


@pytest.mark.parametrize("change", ["message", "edit", "archive", "erase", "shared_edit"])
async def test_reply_uses_original_context_freshness(db, settings, conversation, tmp_path, change):
    settings = settings.model_copy(update={"primary_backend": "claude", "primary_model": "fixture"})
    scope = None
    if change == "shared_edit":
        scope = await db.conversation("owner", "telegram", "-456:7")
        await db.execute(
            "INSERT INTO telegram_destinations VALUES(?,?,?,?,?,?,?)",
            ("fixture", "owner", 99, -456, 7, scope, 0),
        )
    memory = Memory(db, "owner", scope)
    memory_id = await memory.remember("original plan", source="owner")

    class Backend(NativeBackend):
        async def execute(self, request, emit):
            assert "original plan" in request.context
            if change == "message":
                await db.message("owner", conversation, "user", "Wait, change the plan")
            elif change in ("edit", "shared_edit"):
                await memory.edit(memory_id, 1, "new plan", source="owner")
            elif change == "archive":
                await memory.archive(memory_id)
            else:
                await memory.erase(memory_id)
            return ExecutionOutcome(status=Outcome.COMPLETED, text="obsolete answer")

    broker = ToolBroker(db, settings)
    coordinator = Coordinator(
        db, settings, broker, tmp_path / "socket", factory=lambda name: Backend(db, settings)
    )
    try:
        await coordinator.jobs.ingest(conversation, "local", "fixture", {}, "original plan")
        job = await coordinator.jobs.claim("interactive", "fixture")
        await coordinator.run_job(job)
        sent = []

        async def sender(operation, payload):
            sent.append(payload)
            return {"message_id": "obsolete"}

        assert not await Delivery(db, settings).dispatch_one(sender)
        assert not sent
        action = await db.one("SELECT status,error FROM actions")
        assert action["status"] == "cancelled"
        assert action["error"] == ("new_owner_input" if change == "message" else "stale_context")
        assert not await db.one("SELECT 1 FROM messages WHERE role='assistant'")
    finally:
        await broker.close()


@pytest.mark.parametrize("tool", ["forget", "restore"])
@pytest.mark.parametrize("external_change", [False, True])
async def test_memory_tool_acknowledgement_preserves_prior_invalidation(
    db, settings, conversation, tmp_path, tool, external_change
):
    settings = settings.model_copy(update={"primary_backend": "claude", "primary_model": "fixture"})
    memory = Memory(db, "owner")
    memory_id = await memory.remember("original plan", source="owner")
    await memory.edit(memory_id, 1, "second plan", source="owner")
    other_conversation = await db.conversation("owner", "local", "other")
    other_context = await ContextAssembler(db, "owner", settings.context_window).assemble(
        other_conversation, "plan"
    )
    broker = ToolBroker(db, settings)

    class Backend(NativeBackend):
        async def execute(self, request, emit):
            assert "second plan" in request.context
            if external_change:
                await memory.edit(memory_id, 2, "external update", source="owner")
            arguments = {"id": memory_id}
            if tool == "restore":
                arguments["revision"] = 1
            result = await broker.call(request.tool_token, tool, arguments)
            assert result.status == "committed"
            return ExecutionOutcome(status=Outcome.COMPLETED, text="Memory change confirmed")

    coordinator = Coordinator(
        db, settings, broker, tmp_path / "socket", factory=lambda name: Backend(db, settings)
    )
    try:
        await coordinator.jobs.ingest(conversation, "local", "fixture", {}, "Change the plan")
        job = await coordinator.jobs.claim("interactive", "fixture")
        await coordinator.run_job(job)
        assert (await db.one("SELECT status FROM jobs WHERE id=?", (job["id"],)))[
            "status"
        ] == "completed"
        sent = []

        async def sender(operation, payload):
            sent.append(payload)
            return {"message_id": "confirmation"}

        assert await Delivery(db, settings).dispatch_one(sender) is not external_change
        action = await db.one("SELECT status,error FROM actions")
        assert action["status"] == ("cancelled" if external_change else "succeeded")
        assert bool(sent) is not external_change
        if external_change:
            assert action["error"] == "stale_context"
        assert (
            await db.one(
                "SELECT invalidated FROM context_snapshots WHERE id=?", (other_context["id"],)
            )
        )["invalidated"] == 1
        current = await db.one(
            "SELECT c.invalidated FROM context_snapshots c JOIN runs r ON r.context_id=c.id WHERE r.job_id=?",
            (job["id"],),
        )
        assert current["invalidated"] == int(external_change)
        record = await memory.show(memory_id)
        assert record["status"] == ("archived" if tool == "forget" else "active")
        if tool == "restore":
            assert record["body"] == "original plan"
    finally:
        await broker.close()
