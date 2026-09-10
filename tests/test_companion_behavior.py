"""Companion behavior depends on durable work, remembered feedback and stable delivery."""

import json
from pathlib import Path

import pytest
from test_maintenance_pipeline import broker_context

from theo.application.coordinator import Coordinator
from theo.backends.base import NativeBackend
from theo.config import Settings
from theo.domain import Conflict, ExecutionOutcome, Outcome
from theo.memory.context import ContextAssembler
from theo.memory.store import Memory
from theo.storage import Database
from theo.tools.broker import ToolBroker
from theo.work.autonomy import CADENCES, Autonomy
from theo.work.goals import Goals
from theo.work.jobs import Jobs
from theo.work.scheduling import Scheduler


async def test_schema_eight_reminder_survives_work_schedule_migration(tmp_path, clock):
    migrations = Path(__file__).parents[1] / "src/theo/migrations"
    old_migrations = tmp_path / "old-migrations"
    old_migrations.mkdir()
    for source in migrations.glob("*.sql"):
        if int(source.name.split("_")[0]) <= 8:
            (old_migrations / source.name).write_bytes(source.read_bytes())
    database = Database(tmp_path / "upgrade", clock)
    try:
        await database.migrate(old_migrations)
        await database.execute(
            "INSERT INTO owners VALUES(?,?,?)", ("owner", "Europe/Dublin", clock())
        )
        conversation = await database.conversation("owner", "local", "upgrade")
        await database.execute(
            "INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "old",
                "owner",
                conversation,
                "Take a walk.",
                "once",
                None,
                None,
                "Europe/Dublin",
                clock() + 60,
                1,
                3600,
                clock(),
            ),
        )
        before = await database.one("SELECT * FROM schedules WHERE id='old'")
        await database.initialize()
        after = await database.one("SELECT * FROM schedules WHERE id='old'")
        assert {key: after[key] for key in before} == before
        assert after["mode"] == "reminder" and after["origin"] == "requested"
        await database.initialize()  # Reopening must not reapply or reset the migration.
        clock.advance(60)
        scheduler = Scheduler(database, "owner")
        assert len(await scheduler.tick()) == 1
        assert await scheduler.deliver_reminders(Settings()) == 1
        assert (await database.one("SELECT kind FROM jobs"))["kind"] == "reminder"
        assert not await database.read("PRAGMA foreign_key_check")
    finally:
        await database.close()


async def test_preference_survives_topic_change_and_uses_current_revision(db, conversation):
    memory = Memory(db, "owner")
    preference = await memory.remember(
        "Use concise prose; avoid report headings.", kind="preference", source="message:fixture"
    )
    await memory.edit(preference, 1, "Use concise prose and no jokes.", source="message:correction")
    context = await ContextAssembler(db, "owner", 8000).assemble(conversation, "hey")
    assert "Use concise prose and no jokes." in context["rendered"]
    assert "avoid report headings" not in context["rendered"]
    assert "Use concise prose and no jokes." not in context["instructions"]
    assert {"id": preference, "revision": 2} in context["sources"]["memory"]
    await memory.archive(preference)
    assert preference not in str(
        (await ContextAssembler(db, "owner").assemble(conversation, "hey"))["sources"]
    )


async def test_private_preference_does_not_enter_group_greeting(db):
    private = await Memory(db, "owner").remember(
        "PRIVATE_STYLE_SENTINEL", kind="preference", source="fixture"
    )
    group = await db.conversation("owner", "telegram", "-900:7")
    await db.execute(
        "INSERT INTO telegram_destinations VALUES(?,?,?,?,?,?,?)",
        ("group", "owner", 1, -900, 7, group, 0),
    )
    context = await ContextAssembler(db, "owner").assemble(group, "hey")
    assert private not in str(context["sources"])
    assert "PRIVATE_STYLE_SENTINEL" not in context["rendered"]


async def test_work_schedule_runs_instructions_without_delivering_them(
    db, conversation, clock, tmp_path
):
    settings = Settings(primary_backend="claude", primary_model="fixture")
    scheduler = Scheduler(db, "owner")
    instructions = "INTERNAL: review the archive and decide whether anything needs reporting"
    await scheduler.create(conversation, instructions, due=clock() + 60, mode="work")
    await scheduler.create(conversation, "Time for your walk.", due=clock() + 60)
    clock.advance(60)
    assert len(await scheduler.tick()) == 2
    assert not await scheduler.tick()
    assert await scheduler.deliver_reminders(settings) == 1
    assert (await db.one("SELECT kind FROM jobs WHERE kind='scheduled_work'"))[
        "kind"
    ] == "scheduled_work"

    class Backend(NativeBackend):
        async def execute(self, request, emit):
            assert instructions in request.context
            assert "not sent to chat" in request.instructions
            assert "The event was cancelled" in request.context
            return ExecutionOutcome(
                status=Outcome.COMPLETED, text="Internal note: no further action needed."
            )

    await db.message("owner", conversation, "user", "The event was cancelled")
    broker = ToolBroker(db, settings)
    coordinator = Coordinator(
        db, settings, broker, tmp_path / "socket", factory=lambda name: Backend(db, settings)
    )
    try:
        job = await Jobs(db, "owner").claim("background", "fixture")
        await coordinator.run_job(job)
        assert (await db.one("SELECT status FROM jobs WHERE id=?", (job["id"],)))[
            "status"
        ] == "completed"
        actions = await db.read("SELECT request FROM actions")
        assert len(actions) == 1
        assert json.loads(actions[0]["request"])["text"] == "Time for your walk."
        assert instructions not in str(actions)
    finally:
        await broker.close()


async def test_schedule_work_preserves_autonomous_authority_and_broker_replay(
    db, conversation, clock, tmp_path
):
    broker, token, context = await broker_context(db, Settings(), conversation, tmp_path)
    await db.execute("UPDATE jobs SET origin='autonomous' WHERE id=?", (context.job_id,))
    args = {
        "text": "Check a source and report only a meaningful change",
        "mode": "work",
        "due_at": clock() + 60,
    }
    try:
        first = await broker.call(token, "schedule_task", args)
        assert first.status == "committed"
        assert first.data["mode"] == "work"
        assert first.data["origin"] == "autonomous"
        assert await broker.call(token, "schedule_task", args) == first
        await Jobs(db, "owner").finish(context.job_id, context.generation, Outcome.COMPLETED, {})
        clock.advance(60)
        ids = await Scheduler(db, "owner").tick()
        assert len(ids) == 1
        assert (await db.one("SELECT origin FROM jobs WHERE id=?", (ids[0],)))[
            "origin"
        ] == "autonomous"
        assert await Scheduler(db, "owner").deliver_reminders(Settings()) == 0
    finally:
        await broker.close()


async def test_schedule_identity_cannot_change_message_into_work(db, conversation, clock):
    scheduler = Scheduler(db, "owner")
    await scheduler.create(conversation, "hello", due=clock() + 60, idempotency_key="same")
    with pytest.raises(Conflict):
        await scheduler.create(
            conversation, "hello", due=clock() + 60, idempotency_key="same", mode="work"
        )


async def test_proactive_scan_uses_conversation_then_stays_quiet_without_new_evidence(
    db, conversation, clock
):
    await db.set_control("owner", "background_paused", "false")
    await db.message(
        "owner", conversation, "user", "The permission problem is fixed; please finish the archive."
    )
    autonomy = Autonomy(db, "owner")
    assert (await autonomy.opportunity("proactive_scan"))["reason"] == "conversation_in_progress"
    clock.advance(121)
    result = await autonomy.opportunity("proactive_scan")
    assert result["status"] == "work"
    assert "permission problem is fixed" in result["text"]
    await autonomy.tick(conversation)
    jobs = await db.read("SELECT * FROM jobs WHERE kind='proactive_scan'")
    assert len(jobs) == 1 and jobs[0]["origin"] == "autonomous"
    clock.advance(CADENCES["proactive_scan"])
    await autonomy.tick(conversation)
    assert len(await db.read("SELECT * FROM jobs WHERE kind='proactive_scan'")) == 1
    assert not await db.read("SELECT * FROM actions")
    job = await Jobs(db, "owner").claim("background", "fixture")
    await Jobs(db, "owner").finish(job["id"], job["generation"], Outcome.COMPLETED, {})
    await db.message("owner", conversation, "user", "I also found another page of source notes.")
    clock.advance(CADENCES["proactive_scan"])
    await autonomy.tick(conversation)
    assert len(await db.read("SELECT * FROM jobs WHERE kind='proactive_scan'")) == 2


async def test_goal_step_can_be_revised_through_broker_without_losing_completed_work(
    db, conversation, tmp_path
):
    goals = Goals(db, "owner")
    goal = await goals.create(
        "Review archive",
        "Save sourced lessons",
        conversation,
        [
            {"title": "Locate", "next_action": "Find source"},
            {"title": "Review", "next_action": "Ask for an accessible archive"},
        ],
    )
    before = await goals.inspect(goal)
    await goals.complete_step(before["steps"][0]["id"], "Source located and verified")
    broker, token, _ = await broker_context(db, Settings(), conversation, tmp_path)
    try:
        viewed = await broker.call(token, "goal_inspect", {"id": goal})
        assert viewed.status == "ok"
        step = viewed.data["steps"][1]
        args = {
            "id": step["id"],
            "expected_next_action": step["next_action"],
            "next_action": "Read the now-accessible dated source notes",
        }
        assert (await broker.call(token, "step_update", args)).status == "committed"
        assert (await goals.inspect(goal))["steps"][0]["status"] == "completed"
        with pytest.raises(Conflict):
            await goals.revise_step(step["id"], step["next_action"], "Stale competing edit")
        with pytest.raises(Conflict):
            await goals.revise_step(before["steps"][0]["id"], "Find source", "Redo completed work")
    finally:
        await broker.close()


async def test_host_read_pages_granted_archive_without_approval(db, conversation, tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    source = archive / "preferences.md"
    source.write_text("short natural replies")
    settings = Settings(host_access_enabled=True, host_read_roots=(archive,))
    broker, token, context = await broker_context(db, settings, conversation, tmp_path / "worker")
    try:
        first = await broker.call(token, "host_read", {"path": str(source), "limit": 6})
        assert first.status == "ok" and first.data["text"] == "short "
        second = await broker.call(
            token, "host_read", {"path": str(source), "offset": first.data["next_offset"]}
        )
        assert second.data["text"] == "natural replies" and second.data["next_offset"] is None
        assert not await db.read("SELECT * FROM approvals")
        assert not await db.read("SELECT * FROM actions")
        await Jobs(db, "owner").cancel(context.job_id)
        assert (await broker.call(token, "host_read", {"path": str(source)})).status == "denied"
    finally:
        await broker.close()


@pytest.mark.parametrize("target", ["outside", "core", "symlink", "credential", "fifo"])
async def test_host_read_cannot_escape_grant_or_read_protected_state(
    db, conversation, tmp_path, target
):
    import os

    archive = tmp_path / "archive"
    archive.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("PRIVATE_SENTINEL")
    candidate = outside
    if target == "core":
        candidate = db.root / "private.txt"
        candidate.write_text("PRIVATE_SENTINEL")
    elif target == "symlink":
        candidate = archive / "escaped"
        candidate.symlink_to(outside)
    elif target == "credential":
        candidate = archive / ".env"
        candidate.write_text("PRIVATE_SENTINEL")
    elif target == "fifo":
        candidate = archive / "fifo"
        os.mkfifo(candidate)
    settings = Settings(
        host_access_enabled=True, host_read_roots=(tmp_path if target == "core" else archive,)
    )
    broker, token, _ = await broker_context(db, settings, conversation, tmp_path / "worker")
    try:
        result = await broker.call(token, "host_read", {"path": str(candidate)})
        assert result.status == "denied"
        assert "PRIVATE_SENTINEL" not in result.model_dump_json()
    finally:
        await broker.close()


async def test_replaced_grant_directory_does_not_expand_host_read_authority(
    db, conversation, tmp_path
):
    archive = tmp_path / "archive"
    archive.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.txt").write_text("OUTSIDE_SENTINEL")
    settings = Settings(host_access_enabled=True, host_read_roots=(archive,))
    archive.rename(tmp_path / "old-archive")
    archive.symlink_to(outside, target_is_directory=True)
    broker, token, _ = await broker_context(db, settings, conversation, tmp_path / "worker")
    try:
        result = await broker.call(token, "host_read", {"path": str(archive / "notes.txt")})
        assert result.status == "denied"
    finally:
        await broker.close()


async def test_group_cannot_use_host_reads_or_private_goal_plan(db, tmp_path):
    private = await db.conversation("owner", "local", "owner")
    goal = await Goals(db, "owner").create(
        "Private",
        "Private outcome",
        private,
        [{"title": "Read", "next_action": "Private next action"}],
    )
    group = await db.conversation("owner", "telegram", "-900:7")
    await db.execute(
        "INSERT INTO telegram_destinations VALUES(?,?,?,?,?,?,?)",
        ("group", "owner", 1, -900, 7, group, 0),
    )
    settings = Settings(host_access_enabled=True, host_read_roots=(tmp_path,))
    broker, token, _ = await broker_context(db, settings, group, tmp_path / "worker")
    try:
        for tool, args in [("host_read", {"path": str(tmp_path)}), ("goal_inspect", {"id": goal})]:
            assert (await broker.call(token, tool, args)).status == "denied"
        await db.set_control("owner", "background_paused", "false")
        assert not await Autonomy(db, "owner").tick(group)
    finally:
        await broker.close()
