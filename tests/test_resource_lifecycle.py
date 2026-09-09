"""Regressions for filesystem paths and interrupted transport operations."""

import asyncio
import contextlib
import os
import signal
import sys
from unittest.mock import AsyncMock

import psutil
import pytest

from theo.backends.process import RpcProcess, stop_process
from theo.operations.backups import backup_create, backup_verify
from theo.storage import Database


@pytest.mark.parametrize("parent_exits", [False, True])
async def test_stop_process_reaps_stubborn_children_even_after_leader_exit(tmp_path, parent_exits):
    marker = tmp_path / "child.pid"
    child_code = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    parent_code = (
        "import subprocess,sys,time; from pathlib import Path\n"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}], "
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"while not Path({str(marker)!r}).exists(): time.sleep(0.01)\n"
        + ("sys.exit(0)\n" if parent_exits else "time.sleep(60)\n")
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent_code,
        start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(5):
            while not marker.exists() or not marker.read_text():
                await asyncio.sleep(0.01)
        child = psutil.Process(int(marker.read_text()))
        if parent_exits:
            await asyncio.wait_for(process.wait(), 5)
            assert child.is_running()
        await stop_process(process)
        async with asyncio.timeout(5):
            while child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                await asyncio.sleep(0.01)
        assert process.returncode is not None
        # NativeBackend.cancel and RpcProcess.__aexit__ can both clean up.
        await stop_process(process)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def test_database_and_backups_support_uri_reserved_characters(tmp_path, settings):
    db = Database(tmp_path / "owner ?mode=rw#100%")
    try:
        await db.initialize()
        await db.set_control("owner", "test", "preserved")
        assert await db.control("owner", "test") == "preserved"
        backup = await backup_create(db, settings)
        report = await backup_verify(backup)
        assert report["verified"] and report["database_integrity"] == "ok"
    finally:
        await db.close()


async def test_rpc_send_failure_releases_pending_request(tmp_path):
    rpc = RpcProcess([], tmp_path, {}, AsyncMock(), AsyncMock())
    rpc.send = AsyncMock(side_effect=BrokenPipeError)
    with pytest.raises(BrokenPipeError):
        await rpc.call("test", {})
    assert rpc.pending == {}


async def test_rpc_cancel_during_send_releases_pending_request(tmp_path):
    entered = asyncio.Event()

    async def blocked_send(packet):
        entered.set()
        await asyncio.Future()

    rpc = RpcProcess([], tmp_path, {}, AsyncMock(), AsyncMock())
    rpc.send = blocked_send
    task = asyncio.create_task(rpc.call("test", {}))
    await entered.wait()
    pending = next(iter(rpc.pending.values()))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rpc.pending == {}
    assert pending.cancelled()


async def test_rpc_timeout_also_bounds_a_blocked_send(tmp_path):
    async def blocked_send(packet):
        await asyncio.Future()

    rpc = RpcProcess([], tmp_path, {}, AsyncMock(), AsyncMock())
    rpc.send = blocked_send
    async with asyncio.timeout(1):
        with pytest.raises(TimeoutError):
            await rpc.call("test", {}, timeout=0.01)
    assert rpc.pending == {}
