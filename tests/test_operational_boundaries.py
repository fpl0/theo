import asyncio
import io
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from theo.channels.telegram.adapter import Telegram
from theo.content.artifacts import Artifacts
from theo.delivery.ledger import Delivery
from theo.domain import Conflict, Denied, ToolContext, uid
from theo.execution.files import file_hash
from theo.execution.isolation import launch_options
from theo.execution.workspaces import create_worktree, git, promote_worktree
from theo.memory.context import ContextAssembler
from theo.memory.store import Memory
from theo.operations.qualification import qualification_status
from theo.operations.releases import Releases
from theo.tools.broker import ToolBroker
from theo.tools.registry import REGISTRY
from theo.work.improvement import Critic, Improvement
from theo.work.jobs import Jobs
from theo.work.scheduling import Scheduler


async def test_restore_quarantine_release_requires_review_and_resolved_jobs(
    db, settings, conversation
):
    from theo.operations.releases import release_recovery

    await db.set_control("owner", "quarantined", "true")
    await db.set_control("owner", "recovery_since", str(db.clock()))
    job = await Jobs(db, "owner").enqueue(
        conversation, "delegated", {"text": "restored"}, "restored"
    )
    await db.execute("UPDATE jobs SET status='uncertain' WHERE id=?", (job,))
    with pytest.raises(Denied):
        await release_recovery(db, settings, db.clock())
    await Jobs(db, "owner").cancel(job)
    with pytest.raises(Denied):
        await release_recovery(db, settings, db.clock() - 1)
    assert (await release_recovery(db, settings, db.clock()))["quarantined"] is False
    assert await db.control("owner", "quarantined") == "false"


async def test_repeated_mutation_receipt_and_uncertain_reservation(
    db, conversation, settings, tmp_path
):
    jobs = Jobs(db, "owner")
    job_id = await jobs.enqueue(conversation, "delegated", {"text": "test"}, "test")
    job = await jobs.claim("background", "worker")
    run_id = uid()
    await db.execute(
        "INSERT INTO runs(id,owner_id,job_id,generation,backend,model,status,started_at) VALUES(?,?,?,?,?,?,?,?)",
        (run_id, "owner", job_id, job["generation"], "fixture", "fixture", "running", db.clock()),
    )
    broker = ToolBroker(db, settings)
    token = broker.grant(
        ToolContext(
            owner_id="owner",
            conversation_id=conversation,
            job_id=job_id,
            run_id=run_id,
            generation=job["generation"],
            workspace=tmp_path,
            tools=frozenset(REGISTRY),
        )
    )
    first, second = await asyncio.gather(
        broker.call(token, "remember", {"body": "one persistent memory"}),
        broker.call(token, "remember", {"body": "one persistent memory"}),
    )
    assert first.status in ("committed", "uncertain") and second.status in (
        "committed",
        "uncertain",
    )
    third = await broker.call(token, "remember", {"body": "one persistent memory"})
    assert third.status == "committed"
    assert (await db.one("SELECT count(*) n FROM memory_records"))["n"] == 1


async def test_critic_blocks_unchecked_and_hash_mismatch(db, conversation, settings):
    delivery = Delivery(db, settings)
    action = await delivery.prepare(
        conversation,
        "send_message",
        {"text": "Optional suggestion"},
        "optional",
        autonomous=True,
        discretionary=True,
    )
    sent = []

    async def send(operation, payload):
        sent.append(payload)
        return {"message_id": "1"}

    assert not await delivery.dispatch_one(send)
    assert await Critic(db, "owner").queue() == 1
    row = await db.one("SELECT request_hash FROM actions WHERE id=?", (action,))
    await Critic(db, "owner").record(action, "wrong", '{"verdict":"pass","reason":"fixture"}')
    assert not await delivery.dispatch_one(send)
    await Critic(db, "owner").record(
        action, row["request_hash"], '{"verdict":"pass","reason":"Useful and supported"}'
    )
    assert await delivery.dispatch_one(send)
    assert len(sent) == 1


async def test_skill_needs_evaluation_and_matching_trigger(db, conversation):
    skills = Improvement(db, "owner")
    skill = await skills.propose_skill(
        "Source review", "Always verify primary source dates.", ["research"], "fixture:3"
    )
    with pytest.raises(Denied):
        await skills.activate_skill(skill)
    cases = [
        {
            "input": str(i),
            "expected": "source dates",
            "observed": "source dates checked",
            "passed": True,
        }
        for i in range(3)
    ]
    await skills.evaluate_skill(skill, cases)
    await skills.activate_skill(skill)
    assert (
        "Always verify primary source dates"
        in (await ContextAssembler(db, "owner").assemble(conversation, "research"))["rendered"]
    )
    assert (
        "Always verify primary source dates"
        not in (await ContextAssembler(db, "owner").assemble(conversation, "hello"))["rendered"]
    )
    await skills.rollback_skill(skill)
    assert (
        "Always verify primary source dates"
        not in (await ContextAssembler(db, "owner").assemble(conversation, "research"))["rendered"]
    )


async def test_a27_worktrees_fence_promotion_and_reminder_bypasses_slots(
    db, settings, conversation, tmp_path
):
    repository = tmp_path / "repo"
    repository.mkdir()
    await git(repository, "init")
    await git(repository, "config", "user.email", "fixture@example.invalid")
    await git(repository, "config", "user.name", "Fixture")
    (repository / "initial.txt").write_text("initial")
    await git(repository, "add", ".")
    await git(repository, "commit", "-m", "Initial")
    base = await git(repository, "rev-parse", "HEAD")
    jobs = Jobs(db, "owner")
    ids = []
    for i in range(2):
        conv = await db.conversation("owner", "local", f"coding-{i}")
        ids.append(
            await jobs.enqueue(
                conv, "conversation", {"text": "code"}, f"code-{i}", lane="interactive"
            )
        )
    claimed = [await jobs.claim("interactive", str(i)) for i in range(2)]
    trees = []
    for i, job in enumerate(claimed):
        path = tmp_path / f"work-{i}"
        await create_worktree(db, "owner", job["id"], job["generation"], repository, path)
        (path / f"change-{i}.txt").write_text(str(i))
        await git(path, "add", ".")
        await git(path, "commit", "-m", "Change")
        trees.append(path)
    assert not (trees[0] / "change-1.txt").exists()
    scheduler = Scheduler(db, "owner")
    await scheduler.create(conversation, "Due now", due=db.clock())
    await scheduler.tick()
    assert await scheduler.deliver_reminders(settings) == 1
    assert (await db.one("SELECT count(*) n FROM jobs WHERE status='running'"))["n"] == 2
    await promote_worktree(
        db, "owner", claimed[0]["id"], claimed[0]["generation"], repository, trees[0], base
    )
    with pytest.raises(Conflict):
        await promote_worktree(
            db, "owner", claimed[1]["id"], claimed[1]["generation"], repository, trees[1], base
        )


async def test_a34_sqlite_full_busy_and_broken_statement_leave_writer_usable(db):
    await db.write(lambda c: c.execute("PRAGMA busy_timeout=25").fetchall())
    blocker = sqlite3.connect(db.path)
    blocker.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        await Memory(db, "owner").remember("blocked", source="fixture")
    blocker.rollback()
    blocker.close()
    await db.write(lambda c: c.execute("PRAGMA max_page_count=180").fetchall())
    with pytest.raises(sqlite3.OperationalError, match="full"):
        await Memory(db, "owner").remember("x" * 4_000_000, source="fixture")
    await db.write(lambda c: c.execute("PRAGMA max_page_count=1073741823").fetchall())
    await Memory(db, "owner").remember("writer recovered", source="fixture")
    assert (await Memory(db, "owner").search("recovered"))[0]["body"] == "writer recovered"
    assert (await db.read("PRAGMA integrity_check"))[0]["integrity_check"] == "ok"


@pytest.mark.skipif(
    os.geteuid() != 0 or sys.platform != "linux",
    reason="Linux root-only dedicated-UID boundary canary; not Mac evidence",
)
async def test_a31_real_unprivileged_process_cannot_read_or_write_core(db, settings):
    private = db.root / "secret"
    private.write_text("private")
    private.chmod(0o600)
    candidate = settings.model_copy(
        update={
            "worker_home": Path("/tmp"),
            "isolation_verified": True,
            "runner_uid": 65534,
            "runner_gid": 65534,
        }
    )
    code = "import pathlib,sys;p=pathlib.Path(sys.argv[1]);denied=0\nfor f in (p.read_text,lambda:p.write_text('bad')):\n try:f()\n except PermissionError:denied+=1\nprint(denied)"
    command, options = launch_options(
        candidate, db.root, Path("/tmp"), ["/usr/bin/python3", "-c", code, str(private)]
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd="/tmp",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **options,
        )
    except PermissionError:
        pytest.skip(
            "Host refuses UID transitions; real OS boundary requires target-host verification"
        )
    out, err = await process.communicate()
    assert process.returncode == 0, err
    assert out.strip() == b"2" and private.read_text() == "private"


async def test_a32_release_integrity_and_schema_rollback_gate(db, settings, tmp_path):
    schema = await db.one("SELECT max(version) AS version FROM schema_migrations")
    assert schema
    release = tmp_path / "release"
    release.mkdir()
    (release / "code.py").write_text("version = 1")
    manifest = {
        "id": "test-1",
        "version": "test",
        "source_commit": "fixture",
        "lock_sha256": "fixture",
        "schema_min": 1,
        "schema_max": schema["version"],
        "files": {"code.py": file_hash(release / "code.py")},
        "canary_passed": True,
    }
    (release / "release.json").write_text(json.dumps(manifest))
    manager = Releases(db, settings)
    await manager.stage(release)
    await manager.switch("test-1")
    assert (db.root / "releases/current").resolve().name == "test-1"
    (release / "code.py").write_text("tampered")
    with pytest.raises(ValueError):
        await manager.stage(release)
    manifest["id"] = "bad-schema"
    manifest["schema_max"] = 1
    manifest["files"]["code.py"] = file_hash(release / "code.py")
    (release / "release.json").write_text(json.dumps(manifest))
    await manager.stage(release)
    with pytest.raises(Denied):
        await manager.switch("bad-schema")
    assert (db.root / "releases/current").resolve().name == "test-1"


async def test_qualification_cannot_be_claimed_with_configuration_flags(db, settings):
    forged = settings.model_copy(
        update={
            "qualified_backends": ("claude", "codex"),
            "soak_completed": True,
            "isolation_verified": True,
        }
    )
    assert not (await qualification_status(db, forged))["production_qualified"]


async def test_a38_actual_aiogram_request_models_for_rich_operations(db, settings):
    telegram = Telegram(db, settings, "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    await telegram.bot.session.close()
    calls = []

    class Session:
        async def __call__(self, bot, method, **kwargs):
            calls.append(method.model_dump(exclude_none=True))
            return SimpleNamespace(message_id=7, chat=SimpleNamespace(id=1), date=datetime.now(UTC))

    telegram.bot.session = Session()
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, "PNG")
    image = await Artifacts(db, settings).store(buffer.getvalue(), "photo.png", "fixture")
    examples = {
        "send_message": {"text": "literal <b>& text", "reply_to": 2},
        "edit_message": {"text": "edited", "message_id": 2},
        "delete_message": {"message_id": 2},
        "forward": {"from_chat_id": 1, "message_id": 2},
        "pin": {"message_id": 2},
        "send_photo": {"artifact_id": image["id"], "caption": "photo"},
        "send_document": {"artifact_id": image["id"]},
        "send_location": {"latitude": 53.3, "longitude": -6.2},
        "send_poll": {"question": "Which?", "options": ["A", "B"]},
        "send_buttons": {
            "text": "Source",
            "buttons": [{"text": "Open", "url": "https://example.com"}],
        },
        "react": {"message_id": 2, "emoji": "👍"},
    }
    for operation, payload in examples.items():
        assert (await telegram.send(operation, {"target": "1", **payload}))["message_id"] == 7
    assert calls[0]["reply_parameters"]["message_id"] == 2
    assert calls[1]["message_id"] == 2 and calls[1]["text"] == "edited"
