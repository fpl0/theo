"""Operating authority, pause races and source-job provenance through real SQLite."""

from pathlib import Path

import pytest

from theo.application.commands import ConversationCommands
from theo.config import Settings
from theo.domain import Conflict, Denied, Outcome, ToolContext, uid
from theo.operations.controls import Controls
from theo.operations.qualification import qualification_status
from theo.storage import Database
from theo.tools.broker import ToolBroker
from theo.tools.registry import REGISTRY
from theo.work.jobs import Jobs


@pytest.fixture
def operating_settings(tmp_path):
    return Settings(
        operating_mode="owner_authorized",
        worker_home=tmp_path / "runner",
        isolation_verified=True,
        primary_backend="codex",
        primary_model="fixture",
        model_runtime_controls=(
            "background",
            "autonomy",
            "requested_work",
            "models",
            "deployments",
            "notifications",
        ),
    )


async def test_owner_operation_does_not_fabricate_qualification(
    db, operating_settings, conversation
):
    controls = Controls(db, operating_settings)
    await controls.set("requested_work", False, "Continue my task", conversation=conversation)
    state = await controls.snapshot()
    assert not state["paused"]["requested_work"]
    assert state["paused"]["autonomy"] and state["paused"]["background"]
    assert not (await qualification_status(db, operating_settings))["production_qualified"]
    assert not await db.read("SELECT * FROM qualification_results")
    event = await db.one("SELECT * FROM runtime_control_events")
    assert event["scope"] == "requested_work" and event["revision"] == state["revision"]


async def test_requested_children_keep_authority_in_background_lane(
    db, operating_settings, conversation
):
    jobs = Jobs(db, "owner")
    parent = await jobs.enqueue(
        conversation, "conversation", {"text": "Fix this"}, "owner", lane="interactive"
    )
    child = await jobs.enqueue(
        conversation, "delegated", {"text": "Fix this"}, "child", parent=parent
    )
    other = await db.conversation("owner", "local", "autonomous")
    await jobs.enqueue(other, "reflection", {"text": "Reflect"}, "automatic", origin="autonomous")
    await Controls(db, operating_settings).set("requested_work", False, "Continue requested work")
    claimed = await jobs.claim("background", "worker", honor_operating_controls=True)
    assert claimed["id"] == child and claimed["origin"] == "requested"
    await jobs.finish(child, claimed["generation"], Outcome.COMPLETED, {})
    assert await jobs.claim("background", "worker", honor_operating_controls=True) is None


async def test_autonomous_child_cannot_relabel_its_authority(db, conversation):
    jobs = Jobs(db, "owner")
    parent = await jobs.enqueue(conversation, "reflection", {}, "auto", origin="autonomous")
    child = await jobs.enqueue(conversation, "delegated", {}, "child", parent=parent)
    assert (await db.one("SELECT origin FROM jobs WHERE id=?", (child,)))["origin"] == "autonomous"
    with pytest.raises(Denied, match="authority"):
        await jobs.enqueue(
            conversation, "delegated", {}, "forged", parent=parent, origin="requested"
        )
    assert not await db.one("SELECT id FROM jobs WHERE semantic_key='forged'")


@pytest.mark.parametrize("scope", ["models", "requested_work"])
async def test_claim_rechecks_pause_after_prior_snapshot(
    db, operating_settings, conversation, scope
):
    jobs = Jobs(db, "owner")
    await jobs.enqueue(conversation, "delegated", {}, "task")
    controls = Controls(db, operating_settings)
    await controls.set("background", False, "Start work")
    assert not (await controls.snapshot())["paused"][scope]
    await controls.set(scope, True, "Newer owner pause")
    assert await jobs.claim("background", "worker", honor_operating_controls=True) is None


async def test_old_background_alias_updates_both_controls_atomically(db):
    await db.set_control("owner", "background_paused", "false")
    rows = {row["key"]: row["value"] for row in await db.read("SELECT key,value FROM control")}
    assert rows["autonomy_paused"] == rows["requested_work_paused"] == "false"
    revision = int(rows["runtime_control_revision"])
    await db.set_control("owner", "autonomy_paused", "true")
    assert await db.control("owner", "background_paused") == "true"
    assert await db.control("owner", "requested_work_paused") == "false"
    assert int(await db.control("owner", "runtime_control_revision")) == revision + 1


async def test_qualified_mode_and_missing_isolation_still_block(db, operating_settings):
    with pytest.raises(Denied, match="genuine_seven_day_soak"):
        await Controls(db, Settings()).set("background", False, "Resume")
    with pytest.raises(Denied, match="verified_native_isolation"):
        await Controls(db, operating_settings.model_copy(update={"isolation_verified": False})).set(
            "autonomy", False, "Resume"
        )
    assert await db.control("owner", "background_paused") == "true"


async def test_stale_runtime_revision_cannot_overwrite_a_newer_owner_pause(db, operating_settings):
    controls = Controls(db, operating_settings)
    old_revision = (await controls.snapshot())["revision"]
    await controls.set("models", True, "Owner paused all inference")
    with pytest.raises(Conflict, match="changed"):
        await controls.set("models", False, "Stale resume", expected_revision=old_revision)
    assert (await controls.snapshot())["paused"]["models"]
    assert len(await db.read("SELECT * FROM runtime_control_events")) == 1


async def make_broker(db, settings, conversation, tmp_path, *, origin="requested", scopes=()):
    jobs = Jobs(db, "owner")
    job_id = await jobs.enqueue(conversation, "delegated", {}, uid(), origin=origin)
    job = await jobs.claim("background", "worker")
    run_id = uid()
    await db.execute(
        "INSERT INTO runs(id,owner_id,job_id,generation,backend,model,status,started_at) VALUES(?,?,?,?,?,?,?,?)",
        (run_id, "owner", job_id, job["generation"], "fixture", "fixture", "running", db.clock()),
    )
    context = ToolContext(
        owner_id="owner",
        conversation_id=conversation,
        job_id=job_id,
        run_id=run_id,
        generation=job["generation"],
        workspace=tmp_path,
        tools=frozenset(REGISTRY),
        control_scopes=frozenset(scopes),
    )
    broker = ToolBroker(db, settings)
    return broker, broker.grant(context), context


async def test_runtime_tool_commits_and_replays_one_event(
    db, operating_settings, conversation, tmp_path
):
    broker, token, _ = await make_broker(
        db, operating_settings, conversation, tmp_path, scopes=("requested_work",)
    )
    try:
        args = {
            "scope": "requested_work",
            "paused": False,
            "reason": "Resume the requested task",
            "expected_revision": 0,
        }
        first = await broker.call(token, "runtime_control", args)
        assert first.status == "committed"
        assert (await broker.call(token, "runtime_control", args)) == first
        assert len(await db.read("SELECT * FROM runtime_control_events")) == 1
        assert await db.control("owner", "requested_work_paused") == "false"
        assert await db.control("owner", "autonomy_paused") == "true"
    finally:
        await broker.close()


@pytest.mark.parametrize("case", ["ungranted", "autonomous", "stale", "group", "forged_arguments"])
async def test_runtime_tool_rejects_missing_authority(
    db, operating_settings, conversation, tmp_path, case
):
    if case == "group":
        from theo.channels.telegram.state import TelegramState
        from theo.domain import TelegramDestination

        group_settings = operating_settings.model_copy(
            update={"telegram_destinations": (TelegramDestination(chat_id=-42),)}
        )
        destination = await TelegramState(db, group_settings, bot_id=123).destination(-42)
        assert destination is not None
        conversation = destination
    broker, token, context = await make_broker(
        db,
        operating_settings,
        conversation,
        tmp_path,
        origin="autonomous" if case == "autonomous" else "requested",
        scopes=() if case == "ungranted" else ("requested_work",),
    )
    if case == "stale":
        await Jobs(db, "owner").cancel(context.job_id)
    args = {"scope": "requested_work", "paused": False, "reason": "Resume", "expected_revision": 0}
    if case == "forged_arguments":
        args["control_scopes"] = ["requested_work"]
    try:
        result = await broker.call(token, "runtime_control", args)
        assert result.status in ("denied", "invalid")
        assert await db.control("owner", "requested_work_paused") == "true"
        assert not await db.read("SELECT * FROM runtime_control_events")
    finally:
        await broker.close()


async def test_commands_use_the_same_control_service(db, operating_settings, conversation):
    async def cancel(job):
        raise AssertionError("not cancelling a job")

    commands = ConversationCommands(db, operating_settings, cancel)
    assert "resumed" in await commands.command(conversation, "/resume requested_work")
    assert "paused" in await commands.command(conversation, "/pause deployments")
    assert await db.control("owner", "requested_work_paused") == "false"
    assert await db.control("owner", "deployments_paused") == "true"
    assert await db.control("owner", "autonomy_paused") == "true"


@pytest.mark.parametrize("paused", ["true", "false"])
async def test_upgrade_preserves_existing_pause_and_descendant_origin(tmp_path, clock, paused):
    import hashlib
    import sqlite3

    root = tmp_path / "old"
    root.mkdir()
    migrations = Path(__file__).parents[1] / "src/theo/migrations"
    connection = sqlite3.connect(root / "theo.sqlite3")
    try:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT,applied_at REAL)"
        )
        for path in sorted(migrations.glob("*.sql")):
            version = int(path.name.split("_")[0])
            if version >= 6:
                break
            connection.executescript(path.read_text())
            connection.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?)",
                (version, hashlib.sha256(path.read_bytes()).hexdigest(), clock()),
            )
        connection.execute("INSERT INTO owners VALUES('owner','Europe/Dublin',?)", (clock(),))
        connection.execute("INSERT INTO control VALUES('owner','background_paused',?)", (paused,))
        connection.execute(
            "INSERT INTO conversations(id,owner_id,channel,target) VALUES('conv','owner','local','owner')"
        )
        for job_id, parent, key in (
            ("parent", None, "autonomy:reflection:source"),
            ("child", "parent", "delegate:parent"),
        ):
            connection.execute(
                "INSERT INTO jobs(id,owner_id,conversation_id,parent_id,root_id,kind,lane,status,payload,semantic_key,deadline,available_at,created_at,updated_at) VALUES(?,'owner','conv',?,'parent','delegated','background','queued','{}',?,?,0,0,0)",
                (job_id, parent, key, clock() + 100),
            )
        connection.commit()
    finally:
        connection.close()
    upgraded = Database(root, clock)
    try:
        await upgraded.initialize()
        await upgraded.initialize()
        assert await upgraded.control("owner", "autonomy_paused") == paused
        assert await upgraded.control("owner", "requested_work_paused") == paused
        assert all(
            row["origin"] == "autonomous" for row in await upgraded.read("SELECT origin FROM jobs")
        )
        assert not await upgraded.read("PRAGMA foreign_key_check")
    finally:
        await upgraded.close()
