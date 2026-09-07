import asyncio
import json
import plistlib
import sqlite3
import sys
from datetime import UTC, datetime

import pytest

from theo.artifacts import Artifacts
from theo.autonomy import CADENCES, Autonomy
from theo.domain import Conflict
from theo.goals import Goals
from theo.importer import import_luke
from theo.memory import Memory
from theo.operations import backup_create, backup_verify, restore_backup
from theo.scheduling import Scheduler, next_cron
from theo.storage import Database
from theo.supervisor import service_definition


def test_a22_dst_gap_skipped_fold_once_earlier():
    before_gap = datetime(2026, 3, 28, 12, tzinfo=UTC).timestamp()
    next_due = datetime.fromtimestamp(next_cron("30 1 * * *", "Europe/Dublin", before_gap), UTC)
    assert next_due == datetime(2026, 3, 30, 0, 30, tzinfo=UTC)
    before_fold = datetime(2026, 10, 24, 12, tzinfo=UTC).timestamp()
    first = next_cron("30 1 * * *", "Europe/Dublin", before_fold)
    assert datetime.fromtimestamp(first, UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert (
        datetime.fromtimestamp(next_cron("30 1 * * *", "Europe/Dublin", first), UTC)
        .date()
        .isoformat()
        == "2026-10-26"
    )


async def test_a21_outage_coalesces_intervals_and_never_loses_once(db, conversation, clock):
    scheduler = Scheduler(db, "owner")
    await scheduler.create(conversation, "maintenance", interval=60)
    await scheduler.create(conversation, "once", due=clock() + 10)
    await scheduler.create(conversation, "daily", cron="0 9 * * *")
    clock.advance(14 * 86400 + 2 * 3600)
    jobs = await scheduler.tick()
    assert len(jobs) in (2, 3)
    assert len(await scheduler.tick()) == 0
    rows = await db.read("SELECT payload FROM jobs")
    assert any(json.loads(row["payload"])["text"] == "once" for row in rows)
    assert len([row for row in rows if json.loads(row["payload"])["text"] == "maintenance"]) == 1


async def test_a25_goal_cannot_complete_without_steps_or_evidence(db, conversation):
    goals = Goals(db, "owner")
    empty = await goals.create("Research", "Cited report", conversation, [])
    with pytest.raises(Conflict):
        await goals.update(empty, "completed", evidence="model said done")
    goal = await goals.create(
        "Research",
        "Cited report",
        conversation,
        [{"title": "Inspect", "next_action": "Read the source"}],
    )
    with pytest.raises(Conflict):
        await goals.update(goal, "completed", evidence="report")
    step = await db.one("SELECT id FROM plan_steps WHERE goal_id=?", (goal,))
    await goals.complete_step(step["id"], "artifact:validated-report")
    await goals.update(goal, "completed", evidence="artifact:validated-report")
    assert (await db.one("SELECT status FROM goals WHERE id=?", (goal,)))["status"] == "completed"


@pytest.mark.parametrize("kind", list(CADENCES))
async def test_a26_each_autonomy_loop_has_a_typed_noop_on_empty_evidence(db, kind):
    await db.set_control("owner", "background_paused", "false")
    result = await Autonomy(db, "owner").opportunity(kind)
    assert result["status"] == "noop"
    assert result["reason"]


async def test_a26_autonomy_produces_work_from_real_goal_and_failure(db, conversation):
    await db.set_control("owner", "background_paused", "false")
    await Goals(db, "owner").create(
        "Write report",
        "Validated report",
        conversation,
        [{"title": "Read", "next_action": "Read supplied notes"}],
    )
    result = await Autonomy(db, "owner").opportunity("deep_work")
    assert result["status"] == "work"
    assert "Read supplied notes" in result["text"]
    await Autonomy(db, "owner").tick(conversation)
    count = (await db.one("SELECT count(*) n FROM jobs"))["n"]
    await Autonomy(db, "owner").tick(conversation)
    assert (await db.one("SELECT count(*) n FROM jobs"))["n"] == count


async def test_a35_backup_while_writing_and_restore_quarantine(
    db, conversation, settings, tmp_path
):
    memory = Memory(db, "owner")
    await memory.remember("durable telescope secret", source="owner")

    async def writes():
        for i in range(15):
            await memory.remember(f"concurrent write {i}", source="fixture")

    backup, _ = await asyncio.gather(backup_create(db, settings), writes())
    assert (await backup_verify(backup))["verified"]
    target = tmp_path / "restored"
    report = await restore_backup(backup, target, settings)
    assert report["outbound"] == "quarantined"
    restored = Database(target)
    try:
        assert await restored.control("owner", "quarantined") == "true"
        assert await Memory(restored, "owner").search("telescope")
    finally:
        await restored.close()


async def test_backup_includes_exact_external_blob_manifest(db, settings, tmp_path):
    small = settings.model_copy(update={"inline_blob_limit": 1024})
    artifact = await Artifacts(db, small).store(b"a" * 4096, "text.txt", "Fixture")
    backup = await backup_create(db, small)
    assert (await backup_verify(backup))["external_blobs"] == 1
    manifest = json.loads((backup / "manifest.json").read_text())
    blob = backup / manifest["blobs"][0]["location"]
    blob.write_bytes(b"tampered")
    with pytest.raises(ValueError):
        await backup_verify(backup)
    assert (await Artifacts(db, small).content(artifact["id"]))[1] == b"a" * 4096


async def test_a36_import_idempotent_tombstones_conflicts_and_no_live_dependency(db, tmp_path):
    source = tmp_path / "luke-snapshot"
    notes = source / "memory/entity"
    notes.mkdir(parents=True)
    (notes / "first.md").write_text("---\nid: first\n---\nArchived full body")
    (notes / "conflict.md").write_text("file version")
    connection = sqlite3.connect(source / "luke.db")
    connection.executescript(
        "CREATE TABLE memory_meta(id TEXT,type TEXT,status TEXT); CREATE TABLE memory_fts(id TEXT,type TEXT,content TEXT); INSERT INTO memory_meta VALUES('first','entity','archived'); INSERT INTO memory_meta VALUES('conflict','entity','active'); INSERT INTO memory_fts VALUES('first','entity','Archived full body'); INSERT INTO memory_fts VALUES('conflict','entity','database version');"
    )
    connection.close()
    dry = await import_luke(db, "owner", source)
    assert dry["counts"]["accepted"] == 1
    assert dry["counts"]["quarantined"] == 1
    first = await import_luke(db, "owner", source, True)
    second = await import_luke(db, "owner", source, True)
    assert first["imported"] == 1 and first["source_unchanged"]
    assert second["imported"] == 0
    row = await db.one("SELECT * FROM memory_records")
    assert row["status"] == "archived"
    import shutil

    shutil.rmtree(source)
    await Memory(db, "owner").restore(row["id"])
    assert (await Memory(db, "owner").search("Archived"))[0]["body"] == "Archived full body"


def test_service_definition_is_parameterized(tmp_path):
    data = plistlib.loads(service_definition(tmp_path, tmp_path / "python"))
    assert data["ProgramArguments"][-1] == str(tmp_path)
    assert data["KeepAlive"] is True
    assert "luke" not in json.dumps(data).lower()


async def test_a40_cli_clean_install_memory_and_doctor(tmp_path):
    root = tmp_path / "new-root"

    async def run(*arguments):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "theo",
            "--data-root",
            str(root),
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        return json.loads(stdout)

    assert (await run("init"))["initialized"]
    assert (await run("init"))["existing_configuration_preserved"]
    stored = await run("memory", "remember", "CLI sentinel")
    assert (await run("memory", "show", stored["id"]))["body"] == "CLI sentinel"
    assert (await run("doctor", "--json"))["production_qualified"] is False
