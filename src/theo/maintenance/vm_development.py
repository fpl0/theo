"""Provide coding jobs a complete offline environment built in a disposable VM.

Candidate installers run under the guest build identity. After sealed transfer,
the controller only copies bytes and supplies a literal source path for editable
imports. The prepared environment is writable by the job's shared workspace group;
it is never an accepted deployment bundle or a controller interpreter.
"""

import asyncio
import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path

from theo.domain import Denied
from theo.maintenance.bundle_archive import extract
from theo.maintenance.bundles import inventory
from theo.maintenance.configuration import ControllerConfig
from theo.maintenance.source import source_digest
from theo.maintenance.vm_driver import VmDriver
from theo.maintenance.vm_packaging import VmPackager


def editable(prefix: Path, workspace: Path) -> None:
    """Replace the installed project with a literal source search path, without hooks."""
    source = workspace / "src"
    if source.is_symlink() or not source.is_dir() or any(ord(c) < 32 for c in str(source)):
        raise Denied("An editable Theo workspace needs its own regular source directory")
    site = prefix / "lib/python3.14/site-packages"
    package = site / "theo"
    if package.is_symlink() or not package.is_dir():
        raise Denied("The development environment is missing the installed Theo package")
    shutil.rmtree(package)
    # A .pth file containing only an absolute directory is data, not an import
    # statement. Existing wheel metadata and the relocated console script remain.
    projection = site / "_theo_workspace.pth"
    if projection.exists() or projection.is_symlink():
        raise Denied("The dependency environment already claims the workspace projection")
    projection.write_text(str(source.resolve()) + "\n")
    for path in (prefix, *prefix.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            path.chmod(0o770)
        elif stat.S_ISREG(mode):
            path.chmod(0o770 if mode & 0o111 else 0o660)


async def prepare_environment(config: ControllerConfig, workspace: Path) -> Path:
    if (
        workspace.is_symlink()
        or not workspace.resolve().is_relative_to(config.workspaces.resolve())
        or workspace.resolve() == config.workspaces.resolve()
    ):
        raise Denied("Development preparation requires one controller-issued coding workspace")
    expected = source_digest(workspace)
    lock_hash = hashlib.sha256((workspace / "uv.lock").read_bytes()).hexdigest()
    builder = VmPackager(config)
    async with VmDriver(builder.vm_settings) as vm:
        await vm.prepare(workspace, config.dependency_wheels)
        await builder.build_prefixes(vm, None, expected, development=True)
        archive, receipt = await vm.export_bundle(expected)
    if source_digest(workspace) != expected:
        raise Denied("The coding workspace changed before its editing job was admitted")
    scratch = workspace / ".theo"
    if scratch.is_symlink() or (scratch.exists() and not scratch.is_dir()):
        raise Denied("Development scratch space cannot redirect controller writes")
    # Repair workspaces already hold the previous patch here. Editing has not
    # been admitted, so preserve that controller-created repair evidence.
    scratch.mkdir(mode=0o770, exist_ok=True)
    staging = scratch / "runtime-staging"
    await asyncio.to_thread(extract, archive, staging)
    if set(path.name for path in staging.iterdir()) != {"core", "worker", "uv.lock"} or any(
        (staging / name).is_symlink() or not (staging / name).is_dir()
        for name in ("core", "worker")
    ):
        raise Denied("Development output must contain only its two prefixes and dependency lock")
    if hashlib.sha256((staging / "uv.lock").read_bytes()).hexdigest() != lock_hash:
        raise Denied("The development environment does not match the issued dependency lock")
    if await asyncio.to_thread(inventory, staging / "core") != await asyncio.to_thread(
        inventory, staging / "worker"
    ):
        raise Denied("Development prefixes differ after their relocation checks")
    prefix = scratch / "environment"
    (staging / "worker").rename(prefix)
    await asyncio.to_thread(editable, prefix, workspace)
    await asyncio.to_thread(shutil.rmtree, staging)
    (workspace / ".venv").symlink_to(".theo/environment", target_is_directory=True)
    records = config.root / "development-environments"
    records.mkdir(parents=True, exist_ok=True)
    with (records / (uuid.uuid4().hex + ".json")).open("x") as stream:
        json.dump(
            {
                "workspace": str(workspace),
                "source_sha256": expected,
                "lock_sha256": lock_hash,
                "export": receipt,
                "checks": builder.receipts,
                "vm_stopped": True,
            },
            stream,
        )
        stream.flush()
        os.fsync(stream.fileno())
    return prefix / "bin/python"
