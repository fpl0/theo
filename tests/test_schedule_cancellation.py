import asyncio

import pytest

from theo.delivery.contracts import NoEffect
from theo.delivery.ledger import Delivery
from theo.work.scheduling import Scheduler


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("operation", ["cancel", "reschedule"])
async def test_schedule_change_stops_admitted_occurrence(
    db, conversation, settings, clock, prepared, operation
):
    scheduler = Scheduler(db, "owner")
    schedule = await scheduler.create(conversation, "obsolete reminder", due=clock() + 1)
    clock.advance(1)
    await scheduler.tick()
    if prepared:
        await scheduler.deliver_reminders(settings)
    if operation == "cancel":
        await scheduler.cancel(schedule)
    else:
        await scheduler.reschedule(schedule, clock() + 60)
    await scheduler.deliver_reminders(settings)
    sent = []

    async def sender(operation, payload):
        sent.append(payload)
        return {"message_id": "received"}

    assert not await Delivery(db, settings).dispatch_one(sender)
    assert not sent
    if operation == "reschedule":
        clock.advance(60)
        assert len(await scheduler.tick()) == 1
        await scheduler.deliver_reminders(settings)
        assert await Delivery(db, settings).dispatch_one(sender)
        assert len(sent) == 1


@pytest.mark.parametrize("outcome", ["rejected", "uncertain", "accepted"])
@pytest.mark.parametrize("operation", ["cancel", "reschedule"])
async def test_schedule_change_during_send_keeps_receipts_without_retrying(
    db, conversation, settings, clock, outcome, operation
):
    scheduler = Scheduler(db, "owner")
    schedule = await scheduler.create(conversation, "reminder", due=clock() + 1)
    clock.advance(1)
    await scheduler.tick()
    await scheduler.deliver_reminders(settings)
    delivery = Delivery(db, settings)
    started, release = asyncio.Event(), asyncio.Event()

    async def sender(operation, payload):
        started.set()
        await release.wait()
        if outcome == "rejected":
            raise NoEffect("rate_limited", retry_after=1)
        if outcome == "uncertain":
            raise TimeoutError("acknowledgement lost")
        return {"message_id": "received"}

    dispatch = asyncio.create_task(delivery.dispatch_one(sender))
    await started.wait()
    try:
        if operation == "cancel":
            await scheduler.cancel(schedule)
        else:
            await scheduler.reschedule(schedule, clock() + 60)
    finally:
        release.set()
        await dispatch
    action = await db.one("SELECT * FROM actions")
    if outcome == "uncertain":
        assert action["status"] == "uncertain"
        await delivery.reconcile(action["id"], confirmed_no_effect=True)
    expected = "succeeded" if outcome == "accepted" else "cancelled"
    assert (await db.one("SELECT status FROM actions"))["status"] == expected
    clock.advance(2)
    assert not await delivery.dispatch_one(sender)
    assert len(await db.read("SELECT * FROM delivery_receipts")) == (outcome == "accepted")
