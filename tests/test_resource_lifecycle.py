"""Regressions for filesystem paths and interrupted transport operations."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from theo.backends.process import RpcProcess
from theo.operations import backup_create, backup_verify
from theo.storage import Database


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
