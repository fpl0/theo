"""Promised goal updates own durable work and are fulfilled only by delivery."""

import pytest
from test_maintenance_pipeline import broker_context

from theo.application.coordinator import Coordinator
from theo.backends.base import NativeBackend
from theo.config import Settings
from theo.delivery.ledger import Delivery
from theo.domain import ExecutionOutcome, Outcome
from theo.tools.broker import ToolBroker
from theo.work.goals import Goals
from theo.work.jobs import Jobs


async def make_goal(db, conversation):
    return await Goals(db, "owner").create(
        "Review synthetic notes",
        "Save a checked summary",
        conversation,
        [{"title": "Review sources", "next_action": "Read the next bounded batch"}],
    )


async def test_checkpoint_is_atomic_replayable_and_preserves_authority(
    db, conversation, clock, tmp_path
):
    goal = await make_goal(db, conversation)
    broker, token, context = await broker_context(db, Settings(), conversation, tmp_path)
    await db.execute("UPDATE jobs SET origin='autonomous' WHERE id=?", (context.job_id,))
    try:
        args = {"id": goal, "next_update_at": clock() + 900}
        result = await broker.call(token, "goal_checkpoint", args)
        assert result.status == "committed"
        assert await broker.call(token, "goal_checkpoint", args) == result
        assert len(await db.read("SELECT * FROM commitments")) == 1
        job = await db.one("SELECT * FROM jobs WHERE id=?", (result.data["job_id"],))
        assert job["kind"] == "goal_checkin" and job["origin"] == "autonomous"
        assert job["available_at"] == clock() + 900
        assert "next_update_local" in result.data
        assert result.data["estimated_completion_at"] is None
        await db.execute("UPDATE jobs SET generation=generation+1 WHERE id=?", (context.job_id,))
        denied = await broker.call(
            token, "goal_checkpoint", {"id": goal, "next_update_at": clock() + 1200}
        )
        assert denied.status == "denied"
        assert len(await db.read("SELECT * FROM commitments")) == 1
    finally:
        await broker.close()


async def test_checkpoint_replaces_future_update_without_cancelling_current_run(
    db, conversation, clock
):
    goal = await make_goal(db, conversation)
    jobs = Jobs(db, "owner")
    parent = await jobs.enqueue(conversation, "conversation", {"text": "Start"}, "parent")
    goals = Goals(db, "owner")
    first = await goals.checkpoint(goal, clock() + 60, parent_job=parent)
    parent_job = await jobs.claim("background", "test")
    await jobs.finish(parent_job["id"], parent_job["generation"], Outcome.COMPLETED, {})
    clock.advance(60)
    current = await jobs.claim("background", "test")
    assert current["id"] == first["job_id"]
    second = await goals.checkpoint(goal, clock() + 600, parent_job=current["id"])
    assert (await db.one("SELECT status FROM jobs WHERE id=?", (current["id"],)))[
        "status"
    ] == "running"
    assert (await db.one("SELECT status FROM commitments WHERE id=?", (first["id"],)))[
        "status"
    ] == "superseded"
    inspected = await goals.inspect(goal)
    assert [item["id"] for item in inspected["checkpoints"]] == [second["id"]]
    await goals.update(goal, "paused", current_job=current["id"])
    assert (await db.one("SELECT status FROM jobs WHERE id=?", (second["job_id"],)))[
        "status"
    ] == "cancelled"


async def test_promised_update_is_fulfilled_only_after_receipted_delivery(
    db, conversation, clock, tmp_path
):
    settings = Settings(primary_backend="claude", primary_model="fixture", autonomous_hour_cap=0)
    goals, jobs = Goals(db, "owner"), Jobs(db, "owner")
    goal = await make_goal(db, conversation)
    parent = await jobs.enqueue(conversation, "conversation", {"text": "Start"}, "parent")
    checkpoint = await goals.checkpoint(goal, clock() + 60, parent_job=parent)
    current = await jobs.claim("background", "test")
    await jobs.finish(current["id"], current["generation"], Outcome.COMPLETED, {})
    assert await jobs.claim("background", "test") is None
    clock.advance(60)
    await db.message("owner", conversation, "user", "The revised source now has 12 entries.")

    class Backend(NativeBackend):
        async def execute(self, request, emit):
            assert "12 entries" in request.context
            assert "promised progress update" in request.context
            return ExecutionOutcome(
                status=Outcome.COMPLETED, text="I checked the first six entries; six remain."
            )

    broker = ToolBroker(db, settings)
    coordinator = Coordinator(
        db, settings, broker, tmp_path / "socket", factory=lambda name: Backend(db, settings)
    )
    try:
        job = await jobs.claim("background", "test")
        await coordinator.run_job(job)
        assert (await db.one("SELECT status FROM commitments WHERE id=?", (checkpoint["id"],)))[
            "status"
        ] == "active"
        sent = []

        async def sender(operation, payload):
            sent.append(payload["text"])
            return {"message_id": "synthetic-checkpoint"}

        assert await Delivery(db, settings).dispatch_one(sender)
        assert sent == ["I checked the first six entries; six remain."]
        assert (await db.one("SELECT status FROM commitments WHERE id=?", (checkpoint["id"],)))[
            "status"
        ] == "fulfilled"
        assert not await Delivery(db, settings).dispatch_one(sender)
    finally:
        await broker.close()


async def test_due_checkpoint_precedes_older_queued_batch(db, conversation, clock):
    goals, jobs = Goals(db, "owner"), Jobs(db, "owner")
    goal = await make_goal(db, conversation)
    batch = await jobs.enqueue(conversation, "deep_work", {}, "older-batch")
    checkpoint = await goals.checkpoint(goal, clock() + 60, parent_job=batch)
    clock.advance(60)
    claimed = await jobs.claim("background", "test")
    assert claimed["id"] == checkpoint["job_id"]
    assert (await db.one("SELECT status FROM jobs WHERE id=?", (batch,)))["status"] == "queued"


@pytest.mark.parametrize("complete", [False, True])
async def test_goal_batch_stays_internal_but_completion_gets_a_final(
    db, conversation, tmp_path, complete
):
    settings = Settings(primary_backend="claude", primary_model="fixture")
    goal = await make_goal(db, conversation)
    goals = Goals(db, "owner")
    state = await goals.inspect(goal)
    jobs = Jobs(db, "owner")
    await jobs.enqueue(
        conversation,
        "deep_work",
        {"text": "Review a batch", "evidence": [{"goal_id": goal}]},
        "batch",
    )

    class Backend(NativeBackend):
        async def execute(self, request, emit):
            assert "internal execution note" in request.instructions
            if complete:
                await goals.complete_step(state["steps"][0]["id"], "All synthetic sources checked")
                await goals.update(goal, "completed", evidence="Saved checked synthetic summary")
            return ExecutionOutcome(
                status=Outcome.COMPLETED,
                text="The checked summary is ready."
                if complete
                else "Internal batch note: six entries remain.",
            )

    broker = ToolBroker(db, settings)
    coordinator = Coordinator(
        db, settings, broker, tmp_path / "socket", factory=lambda name: Backend(db, settings)
    )
    try:
        job = await jobs.claim("background", "test")
        await coordinator.run_job(job)
        actions = await db.read("SELECT operation,request FROM actions")
        assert len(actions) == int(complete)
        assert "Internal batch note" not in str(actions)
    finally:
        await broker.close()


async def test_estimate_requires_basis_and_group_cannot_commit_private_goal(
    db, conversation, clock, tmp_path
):
    goal = await make_goal(db, conversation)
    parent = await Jobs(db, "owner").enqueue(conversation, "conversation", {}, "parent")
    with pytest.raises(ValueError):
        await Goals(db, "owner").checkpoint(
            goal, clock() + 60, parent_job=parent, estimated_completion=clock() + 600
        )
    group = await db.conversation("owner", "telegram", "-990")
    await db.execute(
        "INSERT INTO telegram_destinations VALUES(?,?,?,?,?,?,?)",
        ("group", "owner", 1, -990, 0, group, 0),
    )
    broker, token, _ = await broker_context(db, Settings(), group, tmp_path)
    try:
        denied = await broker.call(
            token, "goal_checkpoint", {"id": goal, "next_update_at": clock() + 60}
        )
        assert denied.status == "denied"
        assert not await db.read("SELECT * FROM commitments")
    finally:
        await broker.close()
