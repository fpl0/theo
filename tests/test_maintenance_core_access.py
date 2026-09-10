"""Real storage and file boundary checks for the unprivileged supervisor helper."""

import fcntl
import json
import os
import stat

import pytest

from theo.domain import Denied
from theo.maintenance.configuration import read_operator_file, read_protected
from theo.maintenance.core_access import operation
from theo.work.jobs import Jobs


async def test_core_helper_observes_actual_file_locks_and_bounded_heartbeat(db, settings):
    assert await operation(db, settings, "daemon_stopped", {}) == {"stopped": True}
    with (db.root / "daemon.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert await operation(db, settings, "daemon_stopped", {}) == {"stopped": False}
    assert await operation(db, settings, "paused", {}) == {"paused": False}
    (db.root / "maintenance.pause").touch()
    assert await operation(db, settings, "paused", {}) == {"paused": True}
    heartbeat = db.root / "heartbeat.json"
    for invalid in ("[]", "null", "{}", " " * 65537 + '{"pid":1,"timestamp":0}'):
        heartbeat.write_text(invalid)
        assert await operation(db, settings, "heartbeat", {}) == {"pid": None, "timestamp": None}
    heartbeat.write_text(json.dumps({"pid": os.getpid(), "timestamp": db.clock(), "secret": "x"}))
    assert await operation(db, settings, "heartbeat", {}) == {
        "pid": os.getpid(),
        "timestamp": db.clock(),
    }


async def test_core_helper_replays_canary_and_only_retries_explicit_eligibility_wait(db, settings):
    body = {"change_id": "change", "bundle_id": "bundle", "deadline": db.clock() + 100}
    first = await operation(db, settings, "canary_create", body)
    assert await operation(db, settings, "canary_create", body) == first
    job_id = first["job_id"]
    assert len(await db.read("SELECT * FROM jobs")) == 1
    job = await Jobs(db, settings.owner_id).claim("interactive", "fixture")
    assert job and job["id"] == job_id
    retry = {"job_id": job_id, "available_at": db.clock(), "deadline": db.clock() + 200}
    await operation(db, settings, "retry_canary", retry)
    assert (await db.one("SELECT generation FROM jobs"))["generation"] == job["generation"]
    await db.execute("UPDATE jobs SET status='waiting_for_quota' WHERE id=?", (job_id,))
    await operation(db, settings, "retry_canary", retry)
    assert await db.one("SELECT status,generation FROM jobs") == {
        "status": "queued",
        "generation": job["generation"] + 1,
    }
    await operation(db, settings, "retry_canary", retry)
    assert (await db.one("SELECT generation FROM jobs"))["generation"] == job["generation"] + 1
    assert await operation(db, settings, "canary_status", {"job_id": job_id}) == {
        "status": "queued",
        "tool_succeeded": False,
    }
    for name, request in (
        ("control", {"key": "owner"}),
        ("draining", {"paused": "false"}),
        ("sql", {}),
    ):
        with pytest.raises(Denied):
            await operation(db, settings, name, request)


def test_operator_configuration_rejects_untrusted_ancestors_and_wrong_file_owner(tmp_path):
    path = tmp_path / "operator.json"
    path.write_text("{}")
    path.chmod(0o600)
    assert read_protected(path, expected_uid=os.geteuid(), private=True) == "{}"
    with pytest.raises(Denied):
        read_protected(path, expected_uid=os.geteuid() + 1)
    # Even a root-owned leaf must not confer authority through /tmp or a user home.
    with pytest.raises(Denied, match="ancestor"):
        read_operator_file(path)
    link = tmp_path / "redirect"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_protected(link)
    path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o020)
    with pytest.raises(Denied):
        read_protected(path)
