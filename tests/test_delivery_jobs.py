import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from theo.delivery import Delivery, NoEffect, split_text
from theo.domain import Conflict, Denied, Outcome
from theo.jobs import Jobs
from theo.memory import Memory
from theo.storage import Database


async def test_a16_inbox_atomic_deduplication_restart(db, conversation):
    jobs = Jobs(db, "owner")
    first = await jobs.ingest(conversation, "telegram", "42", {"text": "hello"}, "hello")
    assert await jobs.ingest(conversation, "telegram", "42", {"text": "hello"}, "hello") is None
    separate = Database(db.root, db.clock)
    try:
        assert (await separate.one("SELECT count(*) n FROM jobs"))["n"] == 1
        assert (await separate.one("SELECT id FROM jobs"))["id"] == first
        assert (await separate.one("SELECT count(*) n FROM messages"))["n"] == 1
    finally:
        await separate.close()


async def test_a28_stale_worker_cannot_finish_or_acquire(db, conversation, clock):
    jobs = Jobs(db, "owner")
    job_id = await jobs.enqueue(conversation, "delegated", {"task": "x"}, "job")
    first = await jobs.claim("background", "worker1")
    clock.advance(61)
    with pytest.raises(Denied):
        await jobs.finish(job_id, first["generation"], Outcome.COMPLETED, {})
    await jobs.recover()
    second = await jobs.claim("background", "worker2")
    assert second["generation"] > first["generation"]
    with pytest.raises(Denied):
        await jobs.resource(job_id, first["generation"], "repo")
    await jobs.finish(job_id, second["generation"], Outcome.COMPLETED, {"evidence": "done"})


async def test_a19_uncertain_send_not_retried(db, conversation, settings):
    delivery = Delivery(db, settings)
    action = await delivery.prepare(conversation, "send_message", {"text": "hello"}, "same")
    sent = []

    async def send(operation, payload):
        sent.append(payload)
        raise TimeoutError("Remote may have accepted")

    assert await delivery.dispatch_one(send)
    assert not await delivery.dispatch_one(send)
    assert len(sent) == 1
    assert (await db.one("SELECT status FROM actions WHERE id=?", (action,)))[
        "status"
    ] == "uncertain"
    await delivery.reconcile(action, receipt={"message_id": 15})
    assert (await db.one("SELECT status FROM actions WHERE id=?", (action,)))[
        "status"
    ] == "succeeded"


async def test_a20_partial_chunks_resume_without_duplicate(db, conversation, settings, clock):
    delivery = Delivery(db, settings)
    text = "a" * 9000
    action = await delivery.prepare(conversation, "send_message", {"text": text}, "long")
    sent = []
    attempts = 0

    async def send(operation, payload):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise NoEffect("rate limit", 2)
        sent.append(payload["text"])
        return {"message_id": attempts}

    assert await delivery.dispatch_one(send)
    assert await delivery.dispatch_one(send)
    assert not await delivery.dispatch_one(send)
    clock.advance(3)
    assert await delivery.dispatch_one(send)
    assert await delivery.dispatch_one(send)
    assert "".join(sent) == text
    assert (await db.one("SELECT status FROM actions WHERE id=?", (action,)))[
        "status"
    ] == "succeeded"
    assert (await db.one("SELECT count(*) n FROM messages WHERE role='assistant'"))["n"] == 1


async def test_approval_binds_hash_target_chat_and_expiry(db, conversation, settings, clock):
    delivery = Delivery(db, settings)
    action = await delivery.prepare(
        conversation, "send_message", {"text": "draft"}, "external", target="999"
    )
    assert (await db.one("SELECT status FROM actions WHERE id=?", (action,)))[
        "status"
    ] == "awaiting_approval"
    approval = await db.one("SELECT * FROM approvals WHERE action_id=?", (action,))
    with pytest.raises(Denied):
        await delivery.decide(approval["id"], "other-chat", True)
    with pytest.raises(Conflict):
        await delivery.prepare(
            conversation, "send_message", {"text": "changed"}, "external", target="999"
        )
    await delivery.decide(approval["id"], conversation, True)
    sent = []

    async def send(operation, payload):
        sent.append(payload)
        return {"message_id": 1}

    await delivery.dispatch_one(send)
    assert sent[0]["target"] == "999"


async def test_a23_correction_invalidates_waiting_draft(db, conversation, settings):
    memory = Memory(db, "owner")
    fact = await memory.set_fact("meeting", "day", "Tuesday", "owner")
    delivery = Delivery(db, settings)
    action = await delivery.prepare(
        conversation, "send_message", {"text": "Tuesday"}, "draft", freshness={fact: 1}
    )
    await memory.set_fact("meeting", "day", "Wednesday", "owner", expected=1)

    async def must_not_send(*args):
        pytest.fail("Stale claim was delivered")

    assert not await delivery.dispatch_one(must_not_send)
    assert (await db.one("SELECT status FROM actions WHERE id=?", (action,)))[
        "status"
    ] == "cancelled"


async def test_a24_interactive_messages_do_not_spend_autonomous_cap(db, conversation, settings):
    delivery = Delivery(db, settings.model_copy(update={"autonomous_hour_cap": 1}))

    async def send(*args):
        return {"message_id": 1}

    for i in range(15):
        await delivery.prepare(conversation, "send_message", {"text": f"Reply {i}"}, f"reply-{i}")
        assert await delivery.dispatch_one(send)
    await delivery.prepare(
        conversation, "send_message", {"text": "Requested reminder"}, "reminder", autonomous=True
    )
    assert await delivery.dispatch_one(send)
    await delivery.prepare(
        conversation, "send_message", {"text": "Second"}, "second", autonomous=True
    )
    assert not await delivery.dispatch_one(send)


async def test_a15_cancellation_revokes_children_but_preserves_dispatched_effect(
    db, conversation, settings
):
    jobs = Jobs(db, "owner")
    parent = await jobs.enqueue(conversation, "delegated", {}, "parent")
    child = await jobs.enqueue(conversation, "delegated", {}, "child", parent=parent)
    running = await jobs.claim("background", "worker")
    action = await Delivery(db, settings).prepare(
        conversation,
        "send_message",
        {"text": "already dispatched"},
        "effect",
        job_id=parent,
        generation=running["generation"],
    )
    await db.execute("UPDATE actions SET status='executing' WHERE id=?", (action,))
    assert set(await jobs.cancel(parent)) == {parent, child}
    await jobs.recover()
    assert (await db.one("SELECT status FROM actions WHERE id=?", (action,)))[
        "status"
    ] == "uncertain"


async def test_background_slot_reserved_for_interactive(db, conversation):
    jobs = Jobs(db, "owner")
    other = await db.conversation("owner", "local", "second")
    third = await db.conversation("owner", "local", "third")
    await jobs.enqueue(conversation, "deep_work", {}, "bg1")
    await jobs.enqueue(other, "deep_work", {}, "bg2")
    await jobs.enqueue(third, "conversation", {}, "user", lane="interactive")
    assert await jobs.claim("background", "bg")
    assert await jobs.claim("background", "bg") is None
    assert await jobs.claim("interactive", "user")


@given(st.text(min_size=1, alphabet=st.characters(blacklist_categories=("Cs",))))
def test_a38_unicode_split_round_trip(text):
    chunks = split_text(text, 32)
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 32 for chunk in chunks)


async def test_a38_caption_overflow_retains_media_first(db, conversation, settings):
    await Delivery(db, settings).prepare(
        conversation, "send_document", {"artifact_id": "existing", "caption": "x" * 5000}, "caption"
    )
    rows = await db.read("SELECT payload FROM outbox ORDER BY ordinal")
    assert json.loads(rows[0]["payload"])["artifact_id"] == "existing"
    assert json.loads(rows[0]["payload"])["caption"] == ""
    assert "".join(json.loads(row["payload"])["text"] for row in rows[1:]) == "x" * 5000
