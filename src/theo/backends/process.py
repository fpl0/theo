"""Bounded stdio JSON-RPC transport and native process-group cleanup.

Correlates requests with replies, handles notifications and releases pending
futures on cancellation or transport failure. Provider protocols live in adapters.
"""

import asyncio
import contextlib
import json
import os
import signal
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from theo.domain import Json, ProtocolError

MAX_FRAME = 1024 * 1024
_stopped_processes: weakref.WeakSet[asyncio.subprocess.Process] = weakref.WeakSet()


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process in _stopped_processes:
        return
    # The session leader may have exited while MCP servers or workers survive.
    # Reap the owned group even then; remember completed cleanup so later calls
    # cannot signal an unrelated process group after the numeric PID is reused.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), 3)
    except TimeoutError:
        pass
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        _stopped_processes.add(process)
    if process.returncode is None:
        await process.wait()


class RpcProcess:
    def __init__(
        self,
        command: list[str],
        cwd: Path,
        env: dict[str, str],
        notify: Callable[[Json], Awaitable[None]],
        request: Callable[[Json], Awaitable[Json]],
        spawn_options: Json | None = None,
    ):
        self.command, self.cwd, self.env = command, cwd, env
        self.notify, self.request = notify, request
        self.spawn_options = spawn_options or {}
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[Json]] = {}
        self.sequence = 0
        self.write_lock = asyncio.Lock()

    async def __aenter__(self) -> RpcProcess:
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=MAX_FRAME,
            **self.spawn_options,
        )
        self.reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.process:
            await stop_process(self.process)
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, ProtocolError):
                await self.reader
        for future in self.pending.values():
            if not future.done():
                future.cancel()

    async def send(self, packet: Json) -> None:
        assert self.process and self.process.stdin
        raw = json.dumps(packet, ensure_ascii=False).encode() + b"\n"
        if len(raw) > MAX_FRAME:
            raise ProtocolError("Outbound protocol frame exceeds bound")
        async with self.write_lock:
            self.process.stdin.write(raw)
            await self.process.stdin.drain()

    async def call(self, method: str, params: Json, timeout: float = 30) -> Json:
        self.sequence += 1
        request_id = self.sequence
        future: asyncio.Future[Json] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            async with asyncio.timeout(timeout):
                await self.send(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                )
                return await future
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        try:
            while line := await self.process.stdout.readline():
                if len(line) > MAX_FRAME:
                    raise ProtocolError("Inbound protocol frame exceeds bound")
                packet: Json = json.loads(line)
                if "method" in packet:
                    if "id" in packet:
                        try:
                            result = await self.request(packet)
                            await self.send(
                                {"jsonrpc": "2.0", "id": packet["id"], "result": result}
                            )
                        except Exception:
                            await self.send(
                                {
                                    "jsonrpc": "2.0",
                                    "id": packet["id"],
                                    "error": {"code": -32601, "message": "Capability denied"},
                                }
                            )
                    else:
                        await self.notify(packet)
                else:
                    future = self.pending.get(packet.get("id", -1))
                    if future and not future.done():
                        if "error" in packet:
                            future.set_exception(
                                ProtocolError(
                                    "Native RPC request failed: "
                                    + str(packet["error"].get("code", "unknown"))
                                )
                            )
                        else:
                            future.set_result(packet.get("result", {}))
            raise ProtocolError("Native runtime closed transport")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, ProtocolError)
                else ProtocolError("Invalid native protocol frame")
            )
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            await self.notify({"method": "theo/transport_error", "params": {"error": str(error)}})
