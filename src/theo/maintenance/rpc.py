"""Bounded authenticated local requests between independently installed services.

Only protected installation configuration supplies endpoints and credentials.
Requests carry no shell commands. Lost replies are retried by durable intent ID.
"""

import asyncio
import contextlib
import hmac
import json
import os
import socket
import struct
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from theo.domain import Conflict, Denied, Json, encode

LIMIT = 1024 * 1024


def peer_uid(writer: asyncio.StreamWriter) -> int:
    """Read the kernel-authenticated effective user of this Unix connection."""
    channel = writer.get_extra_info("socket")
    if channel is None:
        raise Denied("Local peer credentials are unavailable")
    if sys.platform == "darwin":
        # SOL_LOCAL / LOCAL_PEERCRED returns Darwin's xucred (version, uid, groups).
        credentials = channel.getsockopt(0, 1, 76)
        version, uid = struct.unpack_from("=II", credentials)
        if version != 0:
            raise Denied("Unsupported local credential version")
        return uid
    if sys.platform == "linux":
        _, uid, _ = struct.unpack(
            "=iii", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        return uid
    raise Denied("Authenticated maintenance IPC is unsupported on this operating system")


class Client:
    def __init__(self, path: Path, token_file: Path):
        self.path, self.token_file = path, token_file

    async def call(self, operation: str, body: Json) -> Json:
        async with asyncio.timeout(30):
            reader, writer = await asyncio.open_unix_connection(self.path, limit=LIMIT + 1)
            try:
                payload = (
                    encode(
                        {
                            "version": 1,
                            "token": self.token_file.read_text().strip(),
                            "operation": operation,
                            "body": body,
                        }
                    ).encode()
                    + b"\n"
                )
                if len(payload) > LIMIT:
                    raise Denied("Maintenance request exceeds size limit")
                writer.write(payload)
                await writer.drain()
                response = json.loads(await reader.readline())
                if response.get("error"):
                    raise Denied(str(response["error"]))
                return response["result"]
            finally:
                writer.close()
                await writer.wait_closed()


async def serve(
    path: Path,
    token_file: Path,
    handler: Callable[[str, Json], Awaitable[Json]],
    *,
    expected_uid: int | None = None,
) -> asyncio.Server:
    allowed_uid = os.geteuid() if expected_uid is None else expected_uid
    if len(os.fsencode(path)) >= 104:
        raise Denied("Maintenance socket path must be shorter than 104 bytes")
    token = token_file.read_text().strip()
    if len(token) < 32 or token_file.stat().st_mode & 0o007:
        raise Denied("RPC credential must be protected and at least 32 characters")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            _, writer = await asyncio.open_unix_connection(path)
        except ConnectionRefusedError, FileNotFoundError:
            path.unlink(missing_ok=True)
        else:
            writer.close()
            await writer.wait_closed()
            raise Conflict("Maintenance endpoint is already running")

    async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(25):
                if peer_uid(writer) != allowed_uid:
                    raise Denied("Maintenance peer OS identity is not authorized")
                line = await reader.readline()
                if len(line) > LIMIT:
                    raise Denied("Request exceeds limit")
                request = json.loads(line)
                if (
                    set(request) != {"version", "token", "operation", "body"}
                    or request["version"] != 1
                    or not hmac.compare_digest(str(request["token"]), token)
                    or not isinstance(request["body"], dict)
                ):
                    raise Denied("Invalid maintenance authentication or protocol")
                result = {"result": await handler(request["operation"], request["body"])}
        except (Denied, Conflict) as exc:
            result = {"error": str(exc)}
        except Exception as exc:
            result = {"error": type(exc).__name__}
        try:
            raw = encode(result).encode() + b"\n"
            if len(raw) > LIMIT:
                raw = b'{"error":"Response exceeds limit"}\n'
            writer.write(raw)
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    server = await asyncio.start_unix_server(connected, path, limit=LIMIT + 1)
    os.chmod(path, 0o660)
    return server
