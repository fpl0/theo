"""Regressions for filesystem paths and interrupted transport operations."""

import asyncio
import contextlib
import json
import os
import signal
import sys
from unittest.mock import AsyncMock, Mock

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


async def test_native_event_stream_propagates_early_producer_failure(
    db, settings, tmp_path, monkeypatch
):
    from theo.backends.base import NativeBackend
    from theo.domain import ExecutionRequest

    request = ExecutionRequest(
        run_id="fixture",
        job_id="fixture",
        conversation_id="fixture",
        owner_id="owner",
        backend="fixture",
        model="fixture",
        lane="interactive",
        context="fixture",
        workspace=tmp_path,
        deadline=db.clock() + 30,
        generation=1,
        tool_socket="fixture",
        tool_token="fixture",
    )
    monkeypatch.setattr("theo.backends.base.telemetry.carrier", lambda: "fixture")
    monkeypatch.setattr(db, "execute", AsyncMock(side_effect=OSError("database unavailable")))

    async def consume():
        return [event async for event in NativeBackend(db, settings).events(request)]

    task = asyncio.create_task(consume())
    try:
        done, _ = await asyncio.wait({task}, timeout=0.5)
        assert task in done, "Producer failure left the consumer waiting indefinitely"
        with pytest.raises(OSError, match="database unavailable"):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("operation", ["version", "git", "isolation"])
async def test_cancelled_host_probe_reaps_process(db, settings, tmp_path, monkeypatch, operation):
    from theo.backends.base import NativeBackend
    from theo.execution.isolation import verify_isolation
    from theo.execution.workspaces import git

    spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    processes = []

    async def start(*args, **kwargs):
        process = await spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        spawned.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    monkeypatch.setattr(
        "theo.execution.isolation.launch_options", lambda *a, **k: (["fixture"], {})
    )
    if operation == "version":
        work = NativeBackend(db, settings, binary=sys.executable).version()
    elif operation == "git":
        work = git(tmp_path, "status")
    else:
        work = verify_isolation(settings, db.root)
    task = asyncio.create_task(work)
    await spawned.wait()
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert processes[0].returncode is not None
    finally:
        if processes[0].returncode is None:
            processes[0].kill()
        await processes[0].wait()


async def test_daemon_recovery_failure_releases_socket_and_lock(db, settings, monkeypatch):
    import fcntl
    from pathlib import Path

    from theo.application.service import serve
    from theo.tools.broker import ToolBroker

    sockets = []
    listen = ToolBroker.listen

    async def capture(self, path):
        sockets.append(path)
        await listen(self, path)

    monkeypatch.setattr(ToolBroker, "listen", capture)
    monkeypatch.setattr(
        "theo.execution.registry.terminate_registered",
        AsyncMock(side_effect=RuntimeError("recovery failed")),
    )
    for _ in range(2):
        with pytest.raises(RuntimeError, match="recovery failed"):
            await serve(db, settings)
        assert not sockets[-1].exists()
        assert not sockets[-1].parent.exists()
        with (Path(db.root) / "daemon.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("packet", [[], {"token": []}, {"name": []}, {"arguments": []}])
async def test_broker_closes_malformed_packets_without_unhandled_error(db, settings, packet):
    from theo.tools.broker import ToolBroker

    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps(packet) + "\n").encode())
    reader.feed_eof()
    writer = AsyncMock()
    writer.close = Mock()
    writer.write = Mock()
    await ToolBroker(db, settings)._handle(reader, writer)
    writer.wait_closed.assert_awaited_once()


async def test_broker_shutdown_closes_idle_connections(db, settings):
    import tempfile
    from pathlib import Path

    from theo.tools.broker import ToolBroker

    broker = ToolBroker(db, settings)
    with tempfile.TemporaryDirectory(prefix="theo-idle-", dir="/tmp") as directory:
        path = Path(directory) / "broker.sock"
        await broker.listen(path)
        reader, writer = await asyncio.open_unix_connection(str(path))
        try:
            async with asyncio.timeout(1):
                # Complete one exchange so the server has accepted the socket;
                # connect() alone can return while it is still in the backlog.
                writer.write(b'{"token":"unavailable"}\n')
                await writer.drain()
                assert json.loads(await reader.readline())["status"] == "denied"
                await broker.close()
                assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()
