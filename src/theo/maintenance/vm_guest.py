"""Run one bounded recipe as the disposable VM's unprivileged build identity.

This standalone, standard-library runner is copied into a root-owned guest path
by the host driver and invoked with isolated Python. It never runs in the core or
controller host. Requests arrive over the guest-agent stream; candidate processes
cannot rewrite the runner, its inputs, the interpreter, or its result channel.
"""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import pwd
import re
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

ROOT = Path("/private/var/theo-builder")
WORK = ROOT / "work"
TOOLS = ROOT / "tools"
BUILD_UID = 622
OUTPUT_LIMIT = 4 * 1024 * 1024


@dataclass(frozen=True)
class Request:
    argv: tuple[str, ...]
    cwd: Path
    timeout: int
    source_imports: bool
    operation_id: str = "direct"


def request(raw: bytes) -> Request:
    if len(raw) > 32768:
        raise ValueError("VM recipe request exceeded its bound")
    value = cast(object, json.loads(raw))
    if not isinstance(value, dict):
        raise ValueError("VM recipe request must be an object")
    data = cast(dict[str, object], value)
    if set(data) != {"argv", "cwd", "timeout", "source_imports", "operation_id"}:
        raise ValueError("Unknown or missing VM recipe fields")
    argv, cwd, timeout, source_imports = (
        data["argv"],
        data["cwd"],
        data["timeout"],
        data["source_imports"],
    )
    if (
        not isinstance(argv, list)
        or not 1 <= len(cast(list[object], argv)) <= 100
        or any(not isinstance(arg, str) or "\0" in arg for arg in cast(list[object], argv))
        or not isinstance(cwd, str)
        or "\0" in cwd
        or type(timeout) is not int
        or not 1 <= timeout <= 3600
        or type(source_imports) is not bool
    ):
        raise ValueError("Invalid VM recipe request")
    arguments = tuple(cast(list[str], argv))
    identity = operation_id(data["operation_id"])
    directory = Path(cwd)
    if not directory.is_absolute() or not any(
        directory.resolve().is_relative_to(parent) for parent in (WORK, ROOT / "minimum-source")
    ):
        raise ValueError("VM recipe directory must stay in guest work storage")
    if not Path(arguments[0]).is_absolute():
        raise ValueError("VM recipe executable must be absolute")
    return Request(arguments, directory, timeout, source_imports, identity)


def operation_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", value):
        raise ValueError("Invalid guest operation identity")
    return value


def environment(command: Request) -> dict[str, str]:
    result = {
        "HOME": str(WORK / "home"),
        "PATH": f"{TOOLS}/python/bin:{TOOLS}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(WORK / "tmp"),
        "THEO_TEST_SOCKET_ROOT": str(WORK / "tmp"),
        "UV_CACHE_DIR": str(WORK / "cache"),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_PYTHON": str(TOOLS / "python/bin/python3"),
        "UV_OFFLINE": "1",
        "THEO_TEST_OFFLINE": "1",
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYRIGHT_PYTHON_GLOBAL_NODE": "on",
        "HYPOTHESIS_STORAGE_DIRECTORY": str(WORK / "cache/hypothesis"),
    }
    if command.source_imports:
        result["PYTHONPATH"] = str(command.cwd / "src")
    return result


def quiesce() -> None:
    """Reap the guest identity, including descendants that escaped a process group."""
    for _ in range(20):
        killed = subprocess.run(
            ["/usr/bin/pkill", "-KILL", "-u", str(BUILD_UID)], capture_output=True, timeout=3
        )
        if killed.returncode not in (0, 1):
            raise RuntimeError("Could not stop the guest build identity")
        remaining = subprocess.run(
            ["/usr/bin/pgrep", "-u", str(BUILD_UID)], capture_output=True, timeout=3
        )
        if remaining.returncode == 1:
            return
        if remaining.returncode != 0:
            raise RuntimeError("Could not inspect the guest build identity")
        time.sleep(0.05)
    raise RuntimeError("Guest descendants remain; discard this VM")


def check_guest() -> None:
    if sys.platform != "darwin" or os.geteuid() != 0:
        raise RuntimeError("The recipe runner requires a disposable Mac guest")
    model = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], timeout=3).strip()
    if not model.startswith(b"VirtualMac"):
        raise RuntimeError("Refusing to run the guest recipe on a physical host")
    account = pwd.getpwnam("_theobuild")
    if account.pw_uid != BUILD_UID or 80 in os.getgrouplist(account.pw_name, account.pw_gid):
        raise RuntimeError("The guest build account must be a dedicated non-admin identity")
    for path in (ROOT, TOOLS, Path(__file__).resolve()):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("The guest runner and tooling must be root-owned and protected")


def execute(command: Request) -> dict[str, object]:
    check_guest()
    quiesce()
    started = time.monotonic()
    output = bytearray()
    limit: str | None = None
    process = subprocess.Popen(
        command.argv,
        cwd=command.cwd,
        env=environment(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        user=BUILD_UID,
        group=20,
        extra_groups=[],
        umask=0o077,
        start_new_session=True,
    )
    assert process.stdout
    stopped = False

    def interrupted(_number: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            eof = False
            while True:
                if stopped:
                    limit = "cancelled"
                    break
                if time.monotonic() - started >= command.timeout:
                    limit = "timeout"
                    break
                if process.poll() is not None:
                    # A detached process may still hold stdout open. Stop all
                    # guest descendants before waiting for that pipe's EOF.
                    quiesce()
                    if eof:
                        break
                if selector.select(timeout=0.1):
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        eof = True
                        selector.unregister(process.stdout)
                        continue
                    remaining = OUTPUT_LIMIT - len(output)
                    output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        limit = "output"
                        break
    finally:
        quiesce()
        process.wait(timeout=5)
        process.stdout.close()
        signal.signal(signal.SIGTERM, previous)
    return {
        "exit_code": process.returncode,
        "limit": limit,
        "output_base64": base64.b64encode(output).decode("ascii"),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "guest_uid": BUILD_UID,
        "descendants_stopped": True,
    }


def result_root() -> Path:
    check_guest()
    root = ROOT / "results"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != 0 or root.stat().st_mode & 0o077:
        raise RuntimeError("Guest results must remain private to the trusted runner")
    return root


def write_record(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("x") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run_recorded(command: Request) -> dict[str, object]:
    """Keep large logs off the command stream and make completed runs replayable."""
    root = result_root()
    identity = operation_id(command.operation_id)
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "argv": command.argv,
                "cwd": str(command.cwd),
                "timeout": command.timeout,
                "source_imports": command.source_imports,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    with (root / "execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = root / (identity + ".json")
        pending = root / (identity + ".pending")
        if record.exists():
            receipt = cast(dict[str, object], json.loads(record.read_bytes()))
            if receipt.get("request_sha256") != request_hash:
                raise ValueError("Guest operation identity already binds a different request")
            return receipt
        if pending.exists():
            raise RuntimeError("Guest operation has an uncertain outcome; discard this VM")
        write_record(pending, {"request_sha256": request_hash})
        receipt = execute(command)
        output = base64.b64decode(str(receipt.pop("output_base64")), validate=True)
        with (root / (identity + ".log")).open("xb") as stream:
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        receipt.update(
            operation_id=identity,
            request_sha256=request_hash,
            output_bytes=len(output),
            output_sha256=hashlib.sha256(output).hexdigest(),
        )
        write_record(record, receipt)
        return receipt


def read_result(identity: str, offset: int | None = None) -> dict[str, object]:
    root = result_root()
    identity = operation_id(identity)
    receipt = cast(dict[str, object], json.loads((root / (identity + ".json")).read_bytes()))
    if offset is None:
        return receipt
    if not 0 <= offset <= OUTPUT_LIMIT:
        raise ValueError("Invalid guest log offset")
    with (root / (identity + ".log")).open("rb") as stream:
        stream.seek(offset)
        data = stream.read(32768)
    return {
        "operation_id": identity,
        "offset": offset,
        "data_base64": base64.b64encode(data).decode("ascii"),
        "chunk_sha256": hashlib.sha256(data).hexdigest(),
        "output_bytes": receipt["output_bytes"],
        "output_sha256": receipt["output_sha256"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-result")
    parser.add_argument("--offset", type=int)
    args = parser.parse_args()
    if args.offset is not None and args.read_result is None:
        parser.error("A log offset requires its completed operation identity")
    if args.read_result is not None:
        result = read_result(args.read_result, args.offset)
    else:
        result = run_recorded(request(sys.stdin.buffer.read(32769)))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
