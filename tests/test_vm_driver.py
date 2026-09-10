"""Prove VM control bounds and failure cleanup without a host virtualization service."""

import base64
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from pydantic import ValidationError

from theo.domain import Denied
from theo.maintenance import vm_watchdog
from theo.maintenance.configuration import CheckRecipe
from theo.maintenance.contracts import CandidateIdentity, VerificationFailed
from theo.maintenance.source import source_digest
from theo.maintenance.vm_config import VmSettings
from theo.maintenance.vm_driver import GUEST_SOURCE, VmDriver, response
from theo.maintenance.vm_verification import VmVerifier


@pytest.fixture
def vm_settings(tmp_path):
    return VmSettings(
        root=tmp_path / "controller",
        tart_home=tmp_path / "vms",
        tart=tmp_path / "tart",
        tart_sha256="a" * 64,
        base_name="base",
        image_digest="sha256:" + "b" * 64,
        base_files={name: "c" * 64 for name in ("disk.img", "config.json", "nvram.bin")},
        python_archive=tmp_path / "python.tar.gz",
        python_sha256="d" * 64,
        uv=tmp_path / "uv",
        uv_sha256="e" * 64,
        agent=tmp_path / "agent",
        agent_sha256="f" * 64,
    )


def test_vm_configuration_requires_complete_pins_and_resource_headroom(vm_settings):
    for change in (
        {"root": "relative"},
        {"base_name": "../outside"},
        {"base_files": {"disk.img": "a" * 64}},
        {"tart_sha256": "unverified"},
        {"memory_mib": 8192, "max_host_memory_bytes": 8_000_000_000},
        {"cpus": 1},
        {"timeout": 0},
        {"max_input_bytes": 6_000_000_000},
    ):
        with pytest.raises(ValidationError):
            VmSettings.model_validate(vm_settings.model_dump() | change)


def test_malformed_vm_responses_cannot_become_receipts():
    for value in (b"[]", b"null", b"unframed stdout", b'"text"'):
        with pytest.raises(Denied):
            response(value)


async def test_vm_recovery_requires_the_installation_lock(vm_settings):
    with pytest.raises(Denied, match="exclusive installation ownership"):
        await VmDriver(vm_settings).recover()


def test_vm_lock_cannot_redirect_to_another_file(vm_settings, tmp_path):
    vm_settings.root.mkdir(mode=0o700)
    protected = tmp_path / "other-service-state"
    protected.write_text("synthetic protected state")
    (vm_settings.root / "operation.lock").symlink_to(protected)
    with pytest.raises(OSError):
        VmDriver(vm_settings).acquire()
    assert protected.read_text() == "synthetic protected state"


async def test_contending_vm_driver_cannot_recover_or_stop_the_active_owner(vm_settings):
    vm_settings.root.mkdir(mode=0o700)
    contender = VmDriver(vm_settings)
    with (vm_settings.root / "operation.lock").open("a+b") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # If recovery ran, it would try to terminate this test process. Use a
        # deliberately invalid receipt so that any such attempt fails loudly.
        contender.registry.write_text("invalid active owner record")
        with pytest.raises(BlockingIOError):
            await contender.__aenter__()
        assert contender.lock is None
        await contender.close()
        assert contender.registry.read_text() == "invalid active owner record"


@pytest.mark.parametrize(
    "change",
    [
        {"guest_uid": 0},
        {"exit_code": False},
        {"descendants_stopped": False},
        {"request_sha256": "wrong"},
        {"operation_id": "another-command"},
        {"output_bytes": 4 * 1024 * 1024 + 1},
    ],
)
async def test_guest_receipt_must_bind_the_exact_unprivileged_command(vm_settings, change):
    driver = VmDriver(vm_settings)
    driver.directory.mkdir(parents=True)

    async def guest(argv, *, input_file=None, timeout=30):
        request = json.loads(input_file.read_bytes())
        expected = hashlib.sha256(
            json.dumps(
                {k: v for k, v in request.items() if k != "operation_id"}, sort_keys=True
            ).encode()
        ).hexdigest()
        return json.dumps(
            {
                "request_sha256": expected,
                "operation_id": "check-1",
                "guest_uid": 622,
                "exit_code": 0,
                "limit": None,
                "descendants_stopped": True,
                "output_bytes": 0,
                "output_sha256": hashlib.sha256(b"").hexdigest(),
            }
            | change
        ).encode()

    driver.guest = guest
    with pytest.raises(Denied):
        await driver.recipe("check-1", ["/usr/bin/true"], GUEST_SOURCE, 30)
    assert not (driver.directory / "check-1.log").exists()


@pytest.mark.parametrize("tamper", [None, "offset", "data", "operation_id", "output_sha256"])
async def test_guest_logs_are_checked_before_becoming_host_evidence(vm_settings, tamper):
    driver = VmDriver(vm_settings)
    driver.directory.mkdir(parents=True)
    output = b"bounded synthetic evidence" * 2000
    output_hash = hashlib.sha256(output).hexdigest()

    async def guest(argv, *, input_file=None, timeout=30):
        if input_file:
            request = json.loads(input_file.read_bytes())
            expected = hashlib.sha256(
                json.dumps(
                    {k: v for k, v in request.items() if k != "operation_id"}, sort_keys=True
                ).encode()
            ).hexdigest()
            return json.dumps(
                {
                    "request_sha256": expected,
                    "operation_id": "check-1",
                    "guest_uid": 622,
                    "exit_code": 0,
                    "limit": None,
                    "descendants_stopped": True,
                    "output_bytes": len(output),
                    "output_sha256": output_hash,
                }
            ).encode()
        offset = int(argv[-1])
        data = output[offset : offset + 32768]
        chunk = {
            "operation_id": "check-1",
            "offset": offset,
            "data_base64": base64.b64encode(data).decode(),
            "chunk_sha256": hashlib.sha256(data).hexdigest(),
            "output_bytes": len(output),
            "output_sha256": output_hash,
        }
        if tamper == "offset":
            chunk["offset"] = offset + 1
        elif tamper == "data":
            chunk["data_base64"] = base64.b64encode(b"tampered").decode()
        elif tamper:
            chunk[tamper] = "different"
        return json.dumps(chunk).encode()

    driver.guest = guest
    if tamper:
        with pytest.raises(Denied):
            await driver.recipe("check-1", ["/usr/bin/true"], GUEST_SOURCE, 30)
        assert not (driver.directory / "check-1.log").exists()
    else:
        await driver.recipe("check-1", ["/usr/bin/true"], GUEST_SOURCE, 30)
        assert (driver.directory / "check-1.log").read_bytes() == output


@pytest.mark.parametrize("reason", ["timeout", "memory", "disk", "output"])
def test_vm_watchdog_enforces_limits_and_reaps_its_driver(tmp_path, reason):
    registry, limits, log = (
        tmp_path / name for name in ("process.json", "limits.json", "driver.log")
    )
    log.write_bytes(b"x" * (8 * 1024 * 1024 + 1) if reason == "output" else b"")
    limits.write_text(
        json.dumps(
            {
                "timeout": 1 if reason == "timeout" else 30,
                "max_host_memory_bytes": 1 if reason == "memory" else 2**63,
                "min_free_disk_bytes": 2**63 if reason == "disk" else 0,
                "disk_root": str(tmp_path),
                "auxiliary_registry": str(tmp_path / "absent.json"),
                "log": str(log),
            }
        )
    )
    # The real watchdog enforces limits without helper cancellation by the test.
    process = subprocess.run(
        [
            sys.executable,
            "-I",
            vm_watchdog.__file__,
            str(registry),
            str(limits),
            sys.executable,
            "-I",
            "-c",
            "import time; time.sleep(60)",
        ],
        capture_output=True,
        timeout=8,
        env=os.environ.copy(),
    )
    assert process.returncode != 0
    receipt = json.loads(registry.read_bytes())
    assert receipt["stop_reason"] == reason and receipt["finished"]
    with pytest.raises(psutil.NoSuchProcess):
        psutil.Process(receipt["child_pid"])


async def test_failing_vm_check_stops_the_guest_before_returning_its_failure(
    vm_settings, tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "synthetic.py").write_text("value = 1\n")
    candidate = CandidateIdentity(
        change_id="synthetic",
        revision=1,
        base_commit="a" * 40,
        commit="b" * 40,
        tree="c" * 40,
        snapshot_sha256=source_digest(source),
        lock_sha256="d" * 64,
    )
    state = {"stopped": False, "prepared": False}

    class Guest:
        def __init__(self, settings):
            self.directory = tmp_path / "receipt"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            state["stopped"] = True

        async def prepare(self, source, wheels):
            state["prepared"] = True

        async def recipe(self, name, argv, cwd, timeout):
            return {
                "exit_code": 23 if argv == ["/usr/bin/false"] else 0,
                "limit": None,
                "output_sha256": "e" * 64,
                "request_sha256": "f" * 64,
                "guest_uid": 622,
                "descendants_stopped": True,
            }

    monkeypatch.setattr("theo.maintenance.vm_verification.VmDriver", Guest)
    config = SimpleNamespace(
        vm=vm_settings,
        dependency_wheels=Path("/synthetic/wheels"),
        checks=(CheckRecipe(name="behavior", argv=("/usr/bin/false",)),),
    )
    with pytest.raises(VerificationFailed) as failure:
        await VmVerifier(config).check(candidate, source)
    assert state == {"stopped": True, "prepared": True}
    assert failure.value.receipt["name"] == "behavior"
    assert failure.value.receipt["exit_code"] == 23
    assert source_digest(source) == candidate.snapshot_sha256
