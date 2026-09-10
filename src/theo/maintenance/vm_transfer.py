"""Copy a sealed guest archive over a bounded interactive administrative stream.

Each requested offset receives one small, hash-bound response. Reusing the same
connection avoids starting thousands of host and guest processes for large
prefixes. A separately pinned watchdog owns the transport process and deadline.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import cast

from theo.backends.process import stop_process
from theo.domain import Denied, Json


async def errors(reader: asyncio.StreamReader) -> bytes:
    output = bytearray()
    while body := await reader.read(4096):
        output.extend(body)
        if len(output) > 64 * 1024:
            raise Denied("Archive transport error output exceeds its bound")
    return bytes(output)


async def transfer(
    command: list[str],
    environment: dict[str, str],
    registry: Path,
    receipt: Json,
    target: Path,
    *,
    timeout: int = 900,
) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(Path(__file__).with_name("worker_launcher.py")),
        str(registry),
        str(timeout),
        *command,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=64 * 1024,
    )
    assert process.stdin and process.stdout and process.stderr
    error_task = asyncio.create_task(errors(process.stderr))
    offset, checksum = 0, hashlib.sha256()
    size = receipt["archive_bytes"]
    try:
        async with asyncio.timeout(timeout):
            with target.open("xb") as output:
                while offset < size:
                    process.stdin.write(f"{offset}\n".encode())
                    await process.stdin.drain()
                    line = await process.stdout.readline()
                    if not line.endswith(b"\n") or len(line) > 64 * 1024:
                        raise Denied("Truncated or oversized archive stream response")
                    part = cast(Json, json.loads(line))
                    body = base64.b64decode(part["data_base64"], validate=True)
                    if (
                        part.get("offset") != offset
                        or part.get("archive_sha256") != receipt["archive_sha256"]
                        or part.get("archive_bytes") != size
                        or part.get("source_sha256") != receipt["source_sha256"]
                        or not 0 < len(body) <= min(32 * 1024, size - offset)
                        or hashlib.sha256(body).hexdigest() != part.get("chunk_sha256")
                    ):
                        raise Denied("Guest bundle chunk failed its integrity check")
                    output.write(body)
                    checksum.update(body)
                    offset += len(body)
                output.flush()
                os.fsync(output.fileno())
            process.stdin.close()
            if await process.stdout.read(1):
                raise Denied("The archive transport returned unsolicited output")
            await process.wait()
            error = await error_task
            if error:
                target.with_suffix(".error.log").write_bytes(error)
            if process.returncode or checksum.hexdigest() != receipt["archive_sha256"]:
                raise Denied("Transferred bundle differs from the sealed guest output")
    finally:
        process.stdin.close()
        await stop_process(process)
        try:
            error = await asyncio.wait_for(error_task, 1)
            if error:
                target.with_suffix(".error.log").write_bytes(error)
        except TimeoutError:
            error_task.cancel()
        finally:
            with contextlib.suppress(asyncio.CancelledError, Denied):
                await error_task
