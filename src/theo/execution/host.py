"""Owner-authorized host commands with bounded output and explicit identity.

The action ledger dispatches general commands only after exact approval. A fixed
set of diagnostics uses standing permission; candidates cannot classify risk or
inherit the core environment.
"""

import asyncio
import os
from pathlib import Path

from theo.backends.process import stop_process
from theo.config import Settings
from theo.delivery.contracts import NoEffect
from theo.domain import Denied, Json, encode

DIAGNOSTICS = frozenset(
    {
        ("/usr/bin/uname", "-a"),
        ("/usr/bin/id",),
        ("/bin/df", "-h"),
        ("/usr/bin/uptime",),
        ("/usr/bin/sw_vers",),
        ("/usr/bin/vm_stat",),
    }
)


def validate(request: Json) -> None:
    argv = request["argv"]
    if (
        not argv
        or len(argv) > 100
        or any("\x00" in item or len(item) > 16000 for item in argv)
        or not Path(argv[0]).is_absolute()
        or not Path(request["cwd"]).is_absolute()
        or "\x00" in request["cwd"]
    ):
        raise Denied("Host commands require absolute executable and working-directory paths")


def needs_approval(request: Json) -> bool:
    validate(request)
    return (
        tuple(request["argv"]) not in DIAGNOSTICS
        or request["cwd"] != "/"
        or request.get("as_root", False)
    )


async def execute(settings: Settings, request: Json) -> Json:
    """Execute one claimed action; a nonzero exit never establishes no effect."""
    if not settings.host_access_enabled:
        raise NoEffect("Host access has been disabled by the owner")
    try:
        validate(request)
    except Denied as exc:
        raise NoEffect(str(exc)) from None
    if request.get("as_root"):
        if not settings.host_root_launcher:
            raise NoEffect("The approved privileged host launcher is not installed")
        argv = ["/usr/bin/sudo", "-n", "--", str(settings.host_root_launcher)]
    else:
        argv = request["argv"]
    environment = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8",
        "PYTHONNOUSERSITE": "1",
    }
    if os.environ.get("THEO_TEST_OFFLINE") == "1":
        environment["THEO_TEST_OFFLINE"] = "1"
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=request["cwd"],
            env=environment,
            stdin=asyncio.subprocess.PIPE if request.get("as_root") else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise NoEffect("Host command did not start: " + type(exc).__name__) from None
    assert process.stdout
    output = bytearray()
    try:
        async with asyncio.timeout(request["timeout_seconds"]):
            if request.get("as_root"):
                assert process.stdin
                process.stdin.write((encode(request) + "\n").encode())
                await process.stdin.drain()
            while chunk := await process.stdout.read(65536):
                output.extend(chunk)
                if len(output) > 1024 * 1024:
                    raise Denied("Host command output exceeded the limit; effects are uncertain")
            await process.wait()
        return {
            "channel": "host",
            "exit_code": process.returncode,
            "output": output.decode(errors="replace"),
            "as_root": bool(request.get("as_root")),
        }
    finally:
        if request.get("as_root") and process.stdin:
            # EOF is the watchdog's cancellation signal, including a core crash.
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                pass
        await stop_process(process)
