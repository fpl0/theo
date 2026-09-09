"""Behavioral regressions from the durable execution and delivery review."""

import asyncio

import pytest

from theo.application.coordinator import Coordinator
from theo.backends.base import NativeBackend
from theo.cli.commands import execute
from theo.cli.parser import parser
from theo.config import save_settings
from theo.delivery.contracts import NoEffect
from theo.delivery.ledger import Delivery
from theo.domain import Conflict, ExecutionOutcome, Outcome
from theo.operations.backups import backup_create, restore_backup
from theo.operations.releases import release_recovery
from theo.storage import Database
from theo.tools.broker import ToolBroker
from theo.work.jobs import Jobs


@pytest.mark.parametrize("result", ["accepted", "rejected", "unknown", "unknown_no_effect"])
async def test_cancel_during_send_stops_remaining_chunks(db, settings, conversation, clock, result):
    jobs = Jobs(db, "owner")
    job_id = await jobs.enqueue(conversation, "delegated", {}, "cancel-send")
    delivery = Delivery(db, settings)
    action = await delivery.prepare(
        conversation, "send_message", {"text": "a" * 9000}, "multipart", job_id=job_id
    )
    entered, proceed = asyncio.Event(), asyncio.Event()
    sends = []

    async def send(operation, payload):
        sends.append(payload["text"])
        entered.set()
        await proceed.wait()
        if result == "rejected":
            raise NoEffect("rate limit", 1)
        if result.startswith("unknown"):
            raise TimeoutError("remote acceptance unknown")
        return {"message_id": 1}

    task = asyncio.create_task(delivery.dispatch_one(send))
    await entered.wait()
    try:
        await jobs.cancel(job_id)
    finally:
        proceed.set()
        await task
    if result.startswith("unknown"):
        assert (await db.one("SELECT status FROM actions WHERE id=?", (action,)))[
            "status"
        ] == "uncertain"
        if result == "unknown_no_effect":
            await delivery.reconcile(action, confirmed_no_effect=True)
        else:
            await delivery.reconcile(action, receipt={"message_id": 1})
    clock.advance(10)
    assert not await delivery.dispatch_one(send)
    assert len(sends) == 1
    assert (await db.one("SELECT status FROM actions WHERE id=?", (action,)))[
        "status"
    ] == "cancelled"
    assert not await db.one("SELECT id FROM messages WHERE role='assistant'")
    chunks = await db.read(
        "SELECT status FROM outbox WHERE action_id=? ORDER BY ordinal", (action,)
    )
    assert [chunk["status"] for chunk in chunks[1:]] == ["cancelled", "cancelled"]


@pytest.mark.parametrize("text", ["Recovered reply", "x" * 9000], ids=["single", "multipart"])
async def test_reconciled_final_is_recorded_once_in_history(db, settings, conversation, text):
    delivery = Delivery(db, settings)
    action = await delivery.prepare(conversation, "send_message", {"text": text}, "reply")
    chunks = await db.read("SELECT id FROM outbox WHERE action_id=? ORDER BY ordinal", (action,))

    async def accepted(operation, payload):
        return {"message_id": 1}

    async def ambiguous(operation, payload):
        raise TimeoutError("remote acceptance unknown")

    for _ in chunks[:-1]:
        assert await delivery.dispatch_one(accepted)
    assert await delivery.dispatch_one(ambiguous)
    await delivery.reconcile(action, receipt={"message_id": 2})
    messages = await db.read("SELECT content,source FROM messages WHERE role='assistant'")
    assert messages == [{"content": text, "source": action}]
    with pytest.raises(Conflict):
        await delivery.reconcile(action, receipt={"message_id": 2})
    assert not await delivery.dispatch_one(accepted)
    assert len(await db.read("SELECT id FROM delivery_receipts")) == len(chunks)


async def test_restored_multipart_actions_can_be_reconciled_before_release(
    db, settings, conversation, tmp_path
):
    delivery = Delivery(db, settings)
    text = "snapshot response " * 500
    action = await delivery.prepare(conversation, "send_message", {"text": text}, "restore")

    async def send(operation, payload):
        return {"message_id": 1}

    # The first chunk is known delivered at snapshot time; all later chunks may
    # have reached the recipient after that snapshot and must be reviewed.
    await delivery.dispatch_one(send)
    backup = await backup_create(db, settings)
    target = tmp_path / "restored"
    await restore_backup(backup, target, settings)
    restored = Database(target, db.clock)
    try:
        chunks = await restored.read(
            "SELECT id,status FROM outbox WHERE action_id=? ORDER BY ordinal", (action,)
        )
        assert chunks[0]["status"] == "succeeded"
        assert all(chunk["status"] == "uncertain" for chunk in chunks[1:])
        restored_delivery = Delivery(restored, settings)
        with pytest.raises(Conflict):
            await restored_delivery.reconcile(action, receipt={"message_id": 2})
        for index, chunk in enumerate(chunks[1:], 2):
            await restored_delivery.reconcile(
                action, delivery_id=chunk["id"], receipt={"message_id": index}
            )
            if index < len(chunks):
                assert (await restored.one("SELECT status FROM actions WHERE id=?", (action,)))[
                    "status"
                ] == "uncertain"
        assert (await release_recovery(restored, settings, db.clock()))["quarantined"] is False
        assert not await restored_delivery.dispatch_one(send)
        assert (await restored.one("SELECT content FROM messages WHERE source=?", (action,)))[
            "content"
        ] == text
    finally:
        await restored.close()


async def test_failed_interactive_retry_keeps_lane(db, settings, conversation, clock):
    save_settings(db.root, settings)
    jobs = Jobs(db, "owner")
    job_id = await jobs.enqueue(
        conversation, "conversation", {"text": "try again"}, "failed", lane="interactive"
    )
    job = await jobs.claim("interactive", "worker")
    await jobs.finish(job_id, job["generation"], Outcome.FAILED, {"error": "fixture"})
    args = parser().parse_args(["--data-root", str(db.root), "jobs", "retry", job_id])
    result = await execute(args)
    stored = await db.one("SELECT lane,available_at FROM jobs WHERE id=?", (result["job_id"],))
    assert stored["lane"] == "interactive"
    clock.advance(max(0, stored["available_at"] - clock()))
    retried = await jobs.claim("interactive", "worker")
    assert retried is not None
    assert retried["id"] == result["job_id"]


async def test_model_pause_does_not_consume_attempts_and_keeps_reminders_available(
    db, conversation, settings, clock
):
    jobs = Jobs(db, "owner")
    job_id = await jobs.enqueue(conversation, "conversation", {}, "paused", lane="interactive")
    await db.set_control("owner", "models_paused", "true")
    for _ in range(4):
        assert await jobs.claim("interactive", "worker") is None
    assert (await db.one("SELECT status,attempts FROM jobs WHERE id=?", (job_id,))) == {
        "status": "queued",
        "attempts": 0,
    }
    from theo.work.scheduling import Scheduler

    scheduler = Scheduler(db, "owner")
    await scheduler.create(conversation, "Still due", due=clock())
    await scheduler.tick()
    assert await scheduler.deliver_reminders(settings) == 1
    await db.set_control("owner", "models_paused", "false")
    assert (await jobs.claim("interactive", "worker"))["id"] == job_id


async def test_job_lease_is_renewed_during_input_preparation(
    db, settings, conversation, tmp_path, clock, monkeypatch
):
    settings = settings.model_copy(update={"primary_backend": "claude", "primary_model": "fixture"})
    broker = ToolBroker(db, settings)

    class Backend(NativeBackend):
        async def execute(self, request, emit):
            return ExecutionOutcome(status=Outcome.COMPLETED, text="Input processed")

    coordinator = Coordinator(
        db, settings, broker, tmp_path / "socket", factory=lambda name: Backend(db, settings)
    )
    renewed = asyncio.Event()
    prepared = asyncio.Event()

    async def heartbeat(job):
        # Synchronize on an actual DB heartbeat; no wall-clock minute is needed.
        clock.advance(45)
        await coordinator.jobs.heartbeat(job["id"], job["generation"])
        renewed.set()
        await asyncio.Future()

    async def slow_search(text):
        async with asyncio.timeout(0.5):
            await renewed.wait()
        clock.advance(30)
        prepared.set()
        return []

    monkeypatch.setattr(coordinator, "_heartbeat", heartbeat)
    monkeypatch.setattr(coordinator.embeddings, "search", slow_search)
    job_id = await coordinator.jobs.enqueue(
        conversation, "conversation", {"text": "input"}, "input", lane="interactive"
    )
    job = await coordinator.jobs.claim("interactive", "worker")
    await coordinator.run_job(job)
    assert (await db.one("SELECT status FROM jobs WHERE id=?", (job_id,)))["status"] == "completed"
    assert (await db.one("SELECT output FROM runs WHERE job_id=?", (job_id,)))[
        "output"
    ] == "Input processed"
    assert renewed.is_set()
    assert prepared.is_set()


async def test_outbound_identity_includes_recipient(db, settings, conversation, tmp_path):
    from theo.domain import ToolContext, uid
    from theo.tools.registry import REGISTRY

    jobs = Jobs(db, "owner")
    job_id = await jobs.enqueue(conversation, "delegated", {}, "recipients")
    job = await jobs.claim("background", "worker")
    broker = ToolBroker(db, settings)
    token = broker.grant(
        ToolContext(
            owner_id="owner",
            conversation_id=conversation,
            job_id=job_id,
            run_id=uid(),
            generation=job["generation"],
            workspace=tmp_path,
            tools=frozenset(REGISTRY),
        )
    )
    try:
        first = await broker.call(token, "send_message", {"text": "Same message", "target": "100"})
        second = await broker.call(token, "send_message", {"text": "Same message", "target": "200"})
        repeated = await broker.call(
            token, "send_message", {"text": "Same message", "target": "100"}
        )
        assert first.status == second.status == "awaiting_approval"
        assert first.action_id != second.action_id
        assert first.action_id == repeated.action_id
        assert len(await db.read("SELECT id FROM approvals")) == 2
    finally:
        await broker.close()
