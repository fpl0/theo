import fcntl
import json

import pytest

from theo.config import Settings
from theo.domain import Conflict, Denied
from theo.operations.backups import backup_create, backup_verify
from theo.operations.qualification import qualification_status
from theo.operations.releases import Releases


async def test_deployment_exceptions_never_fabricate_qualification(db):
    settings = Settings(
        required_backends=("codex",),
        require_encrypted_storage=False,
        scheduled_backups_enabled=False,
        allow_unencrypted_release_backup=True,
    )
    report = await qualification_status(db, settings)
    assert report["production_qualified"] is False
    assert report["deployment_ready"] is False
    assert "claude_live_canary" not in report["deployment_gates"]
    assert report["deployment_gates"]["genuine_seven_day_soak"] is False
    assert report["gates"]["encrypted_storage"] is False
    assert "encrypted_storage_deferred" in report["deployment_exceptions"]


async def test_unencrypted_exception_only_allows_release_snapshots(db):
    settings = Settings(allow_unencrypted_release_backup=True)
    with pytest.raises(Denied):
        await backup_create(db, settings)
    path = await backup_create(db, settings, release_snapshot=True)
    await backup_verify(path)
    assert json.loads((path / "manifest.json").read_text())["encrypted_storage"] == "not_verified"
    with pytest.raises(Denied):
        await backup_create(db, Settings(), release_snapshot=True)


async def test_release_switch_refuses_live_daemon_before_touching_pointer(db, settings):
    with (db.root / "daemon.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Conflict, match="stop the daemon"):
            await Releases(db, settings).switch("unneeded")
    assert not (db.root / "releases/current").exists()


def test_required_backend_policy_cannot_be_empty():
    with pytest.raises(ValueError):
        Settings(required_backends=())


def test_supervisor_termination_does_not_touch_reused_pid(monkeypatch):
    import psutil

    from theo.execution.processes import terminate_tree

    class ReusedProcess:
        def create_time(self):
            return 999

        def children(self, **kwargs):
            pytest.fail("Must not inspect or signal a reused process")

    monkeypatch.setattr(psutil, "Process", lambda _: ReusedProcess())
    terminate_tree(1234, created_at=123)
