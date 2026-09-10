"""Work inspection reports real jobs without counting its own request or leaking scope."""

import json

import pytest

from theo.application.commands import ConversationCommands
from theo.application.status import status
from theo.domain import ToolContext, uid
from theo.tools.broker import ToolBroker
from theo.tools.registry import REGISTRY
from theo.work.jobs import Jobs


@pytest.fixture
async def status_run(db, settings, conversation, tmp_path):
    jobs = Jobs(db, "owner")
    await jobs.enqueue(
        conversation, "conversation", {"text": "What is queued?"}, "report", lane="interactive"
    )
    job = await jobs.claim("interactive", "fixture")
    broker = ToolBroker(db, settings)
    context = ToolContext(
        owner_id="owner",
        conversation_id=conversation,
        job_id=job["id"],
        run_id=uid(),
        generation=job["generation"],
        workspace=tmp_path,
        tools=frozenset(REGISTRY),
    )
    return broker, broker.grant(context), context


async def test_status_tool_reports_actual_paused_work_and_refreshes(db, settings, status_run):
    broker, token, context = status_run
    jobs = Jobs(db, "owner")
    queued = await jobs.enqueue(
        context.conversation_id, "deep_work", {"text": "Review synthetic notes"}, "background"
    )
    waiting = await jobs.enqueue(
        context.conversation_id, "delegated", {"text": "Waiting on login"}, "waiting"
    )
    await db.execute("UPDATE jobs SET status='waiting_for_auth' WHERE id=?", (waiting,))
    failed = await jobs.enqueue(
        context.conversation_id, "delegated", {"text": "Old failure"}, "failed"
    )
    await db.execute("UPDATE jobs SET status='failed' WHERE id=?", (failed,))
    result = await broker.call(token, "get_status", {})
    assert result.status == "ok"
    assert {row["status"]: row["count"] for row in result.data["jobs"]} == {
        "queued": 1,
        "waiting_for_auth": 1,
        "failed": 1,
    }
    assert result.data["unfinished_total"] == 2
    items = {row["id"]: row for row in result.data["unfinished_jobs"]}
    assert set(items) == {queued, waiting}
    assert items[queued]["summary"] == "Review synthetic notes"
    assert items[queued]["lane"] == "background"
    assert {row["key"]: row["value"] for row in result.data["controls"]}[
        "background_paused"
    ] == "true"
    assert await db.one("SELECT status FROM jobs WHERE id=?", (queued,)) == {"status": "queued"}
    assert not await db.read("SELECT * FROM tool_receipts WHERE job_id=?", (context.job_id,))
    operator = await status(db, settings, exclude_job_id=context.job_id)
    assert operator["jobs"] == result.data["jobs"]
    assert operator["unfinished_jobs"] == result.data["unfinished_jobs"]
    await jobs.cancel(queued)
    fresh = await broker.call(token, "get_status", {})
    assert fresh.status == "ok"
    assert fresh.data["unfinished_total"] == 1
    assert [row["id"] for row in fresh.data["unfinished_jobs"]] == [waiting]


async def test_status_tool_bounds_and_paginates_results(db, status_run):
    broker, token, context = status_run
    expected = []
    for index in range(3):
        expected.append(
            await Jobs(db, "owner").enqueue(
                context.conversation_id, "delegated", {"text": "x" * 2000}, f"item-{index}"
            )
        )
    first = await broker.call(token, "get_status", {"limit": 2})
    second = await broker.call(token, "get_status", {"limit": 2, "offset": 2})
    assert first.status == second.status == "ok"
    assert first.data["has_more"] is True and second.data["has_more"] is False
    assert first.data["unfinished_total"] == second.data["unfinished_total"] == 3
    all_items = first.data["unfinished_jobs"] + second.data["unfinished_jobs"]
    assert sorted(row["id"] for row in all_items) == sorted(expected)
    assert all(len(row["summary"]) == 600 for row in all_items)
    for args in ({"limit": 0}, {"limit": 51}, {"offset": -1}, {"scope": "another-topic"}):
        assert (await broker.call(token, "get_status", args)).status == "invalid"


async def test_status_tool_scope_and_revocation(db, status_run, settings):
    broker, token, context = status_run
    await db.execute(
        "INSERT INTO telegram_destinations VALUES(?,?,?,?,?,?,?)",
        ("group", "owner", 789, -456, 7, context.conversation_id, 0),
    )
    private = await db.conversation("owner", "local", "private")
    other_topic = await db.conversation("owner", "telegram", "-456:8")
    for conv in (private, other_topic):
        await Jobs(db, "owner").enqueue(conv, "deep_work", {"text": "PRIVATE_SECRET"}, conv)
    await db.initialize("other-owner")
    foreign = await db.conversation("other-owner", "local", "other-owner")
    await Jobs(db, "other-owner").enqueue(
        foreign, "delegated", {"text": "OTHER_OWNER_SECRET"}, "foreign"
    )
    visible = await Jobs(db, "owner").enqueue(
        context.conversation_id, "deep_work", {"text": "Shared work"}, "visible"
    )
    # Correction runs retain read access even when previous effects prohibit new mutations.
    await db.execute(
        "UPDATE jobs SET payload=? WHERE id=?",
        (json.dumps({"correction_effects": True}), context.job_id),
    )
    result = await broker.call(token, "get_status", {})
    assert result.status == "ok"
    assert result.data["jobs"] == [{"status": "queued", "count": 1}]
    assert [row["id"] for row in result.data["unfinished_jobs"]] == [visible]
    assert "SECRET" not in json.dumps(result.data)
    operator = await status(
        db, settings, scope=context.conversation_id, exclude_job_id=context.job_id
    )
    assert operator["jobs"] == result.data["jobs"]
    # A private snapshot includes the owner's other work, never another owner's records.
    private_view = await Jobs(db, "owner").inspect(exclude_job_id=context.job_id)
    assert private_view["unfinished_total"] == 3
    assert "OTHER_OWNER_SECRET" not in json.dumps(private_view)
    await Jobs(db, "owner").cancel(context.job_id)
    assert (await broker.call(token, "get_status", {})).status == "denied"
    broker.revoke(context.run_id)
    assert (await broker.call(token, "get_status", {})).status == "denied"


async def test_local_status_command_excludes_itself(db, settings, conversation):
    async def cancel(job_id):
        raise AssertionError("Status must not cancel work")

    job_id = await Jobs(db, "owner").ingest(conversation, "local", "status", {}, "/status")
    await ConversationCommands(db, settings, cancel).process_pending()
    action = await db.one("SELECT request FROM actions WHERE semantic_key=?", (f"final:{job_id}",))
    report = json.loads(json.loads(action["request"])["text"])
    assert report["jobs"] == []
    assert report["unfinished_total"] == 0
    assert report["unfinished_jobs"] == []
    assert (await db.one("SELECT status FROM jobs WHERE id=?", (job_id,)))["status"] == "completed"
