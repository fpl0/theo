import json
import sys

import pytest
from test_maintenance_pipeline import broker_context

from theo.config import Settings
from theo.delivery.ledger import Delivery
from theo.domain import Denied
from theo.execution.host import execute, needs_approval
from theo.tools.schemas import HostCommandArgs


def request(argv, **kwargs):
    return HostCommandArgs(argv=argv, reason="Synthetic host check", **kwargs).model_dump()


@pytest.mark.parametrize(
    "arguments",
    [
        request(["/bin/sh", "-c", "echo example"]),
        request(["/usr/bin/id", "-u"]),
        request(["/usr/bin/id"], cwd="/tmp"),
        request(["/usr/bin/id"], as_root=True),
        request(["/usr/bin/cat", "/private/file"]),
    ],
)
def test_general_commands_cannot_self_classify_as_routine(arguments):
    assert needs_approval(arguments)
    assert not needs_approval(request(["/usr/bin/id"]))


def test_host_executable_must_be_absolute():
    with pytest.raises(Denied):
        needs_approval(request(["id"]))


async def test_host_command_waits_for_exact_approval_and_runs_once(db, conversation, tmp_path):
    settings = Settings(host_access_enabled=True)
    broker, token, _ = await broker_context(db, settings, conversation, tmp_path / "workspace")
    target = tmp_path / "outside-workspace"
    arguments = request(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path(" + repr(str(target)) + ").write_text('changed')",
        ]
    )
    delivery = Delivery(db, settings)

    async def sender(operation, payload):
        assert operation == "host_command"
        return await execute(settings, payload)

    try:
        queued = await broker.call(token, "host_command", arguments)
        assert queued.status == "awaiting_approval"
        assert await broker.call(token, "host_command", arguments) == queued
        assert not await delivery.dispatch_one(sender)
        assert not target.exists()
        await delivery.decide(queued.data["approval_id"], conversation, True)
        assert await delivery.dispatch_one(sender)
        assert target.read_text() == "changed"
        assert not await delivery.dispatch_one(sender)
        row = await db.one("SELECT status,receipt FROM actions WHERE id=?", (queued.action_id,))
        assert row["status"] == "succeeded"
        assert json.loads(row["receipt"])["exit_code"] == 0
    finally:
        await broker.close()


async def test_standing_host_permission_executes_work_without_repeated_approval(
    db, conversation, tmp_path
):
    settings = Settings(host_access_enabled=True, host_command_policy="standing")
    broker, token, _ = await broker_context(db, settings, conversation, tmp_path / "workspace")
    target = tmp_path / "outside-workspace"
    arguments = request(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path(" + repr(str(target)) + ").write_text('done')",
        ]
    )
    delivery = Delivery(db, settings)

    async def sender(operation, payload):
        assert operation == "host_command"
        return await execute(settings, payload)

    try:
        queued = await broker.call(token, "host_command", arguments)
        assert queued.status == "ready" and queued.data["approval_required"] is False
        assert not await db.one("SELECT 1 FROM approvals")
        assert await delivery.dispatch_one(sender)
        assert target.read_text() == "done"
        assert not await delivery.dispatch_one(sender)
        status = await broker.call(token, "action_status", {"id": queued.action_id})
        assert status.data["status"] == "succeeded"

        privileged = await broker.call(
            token, "host_command", request(["/usr/bin/id"], as_root=True)
        )
        assert privileged.status == "ready" and not privileged.data["approval_required"]
        assert not await db.one("SELECT 1 FROM approvals")
        # Revoking broad authority before dispatch must stop a queued privileged effect.
        revoked = Delivery(db, settings.model_copy(update={"host_command_policy": "approval"}))

        async def forbidden(_operation, _payload):
            pytest.fail("Revoked standing authority must not execute")

        assert not await revoked.dispatch_one(forbidden)
        assert (await db.one("SELECT status FROM actions WHERE id=?", (privileged.action_id,)))[
            "status"
        ] == "cancelled"
    finally:
        await broker.close()


async def test_standing_full_host_read_honors_owner_grant(db, conversation, tmp_path):
    settings = Settings(
        host_access_enabled=True, host_command_policy="standing", host_read_roots=(tmp_path,)
    )
    source = tmp_path / ".ssh" / "synthetic-config"
    source.parent.mkdir()
    source.write_text("Synthetic configuration, no credentials")
    broker, token, _ = await broker_context(db, settings, conversation, tmp_path / "workspace")
    try:
        result = await broker.call(token, "host_read", {"path": str(source)})
        assert result.status == "ok" and result.data["text"] == source.read_text()
        assert not await db.one("SELECT 1 FROM approvals")
    finally:
        await broker.close()


async def test_host_authority_and_approval_are_rechecked_at_dispatch(db, conversation, tmp_path):
    settings = Settings(host_access_enabled=True)
    broker, token, _ = await broker_context(db, settings, conversation, tmp_path)
    try:
        queued = await broker.call(token, "host_command", request(["/bin/echo", "safe"]))
        delivery = Delivery(db, settings)
        await delivery.decide(queued.data["approval_id"], conversation, True)
        await db.execute(
            "UPDATE approvals SET decision='rejected' WHERE action_id=?", (queued.action_id,)
        )

        async def forbidden(_operation, _payload):
            pytest.fail("Revoked approval must not execute")

        assert not await delivery.dispatch_one(forbidden)
        assert (await db.one("SELECT status FROM actions WHERE id=?", (queued.action_id,)))[
            "status"
        ] == "cancelled"
        diagnostic = await broker.call(token, "host_command", request(["/usr/bin/id"]))
        assert diagnostic.status == "ready"
        assert not await Delivery(db, Settings()).dispatch_one(forbidden)
        assert (await db.one("SELECT status FROM actions WHERE id=?", (diagnostic.action_id,)))[
            "status"
        ] == "cancelled"
    finally:
        await broker.close()


async def test_host_command_does_not_inherit_core_credentials(monkeypatch):
    monkeypatch.setenv("THEO_TEST_PRIVATE_SENTINEL", "synthetic-private-value")
    result = await execute(
        Settings(host_access_enabled=True),
        request(
            [
                sys.executable,
                "-c",
                "import os; print(os.getenv('THEO_TEST_PRIVATE_SENTINEL','absent'))",
            ]
        ),
    )
    assert result["exit_code"] == 0 and result["output"].strip() == "absent"


@pytest.mark.parametrize("policy", ["approval", "standing"])
async def test_stale_host_job_cannot_create_an_approval(db, conversation, tmp_path, policy):
    settings = Settings(host_access_enabled=True, host_command_policy=policy)
    broker, token, context = await broker_context(db, settings, conversation, tmp_path)
    try:
        await db.execute("UPDATE jobs SET generation=generation+1 WHERE id=?", (context.job_id,))
        result = await broker.call(token, "host_command", request(["/bin/echo", "safe"]))
        assert result.status == "denied"
        assert not await db.one("SELECT 1 FROM approvals")
    finally:
        await broker.close()


@pytest.mark.parametrize("policy", ["approval", "standing"])
async def test_host_command_is_private_owner_only(db, tmp_path, policy):
    conversation = await db.conversation("owner", "telegram", "-10001")
    await db.execute(
        "INSERT INTO telegram_destinations(id,owner_id,bot_id,chat_id,topic_id,conversation_id,private) VALUES(?,?,?,?,?,?,?)",
        ("destination", "owner", 1, -10001, 0, conversation, 0),
    )
    broker, token, _ = await broker_context(
        db, Settings(host_access_enabled=True, host_command_policy=policy), conversation, tmp_path
    )
    try:
        result = await broker.call(token, "host_command", request(["/usr/bin/id"]))
        assert result.status == "denied"
        assert not await db.one("SELECT 1 FROM actions")
    finally:
        await broker.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Actual Mac inherited sandbox boundary")
async def test_native_workspace_cannot_invoke_privileged_host_transport(tmp_path, monkeypatch):
    from theo.execution.workspaces import execute_scoped

    monkeypatch.delenv("NODE_OPTIONS", raising=False)

    root = tmp_path / "core"
    root.mkdir()
    runner = tmp_path / "runner"
    workspace = runner / "workspaces/job"
    workspace.mkdir(parents=True)
    result = await execute_scoped(
        Settings(worker_home=runner, isolation_verified=True),
        root,
        workspace,
        ["/bin/sh", "-c", "/usr/bin/sudo -n /usr/bin/id"],
    )
    assert result["exit_code"] != 0
    assert "Operation not permitted" in result["output"]
