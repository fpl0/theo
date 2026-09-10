"""Own a VM driver and enforce its limits independently of the controller.

Only the recorded driver process group is signalled. Virtualization services are
observed for conservative memory accounting; unrelated host processes are never
terminated. Guest root storage is fixed-size and host free space is rechecked.
"""

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import psutil

SERVICE_PREFIXES = ("com.apple.Virtualization.", "com.apple.gpusw.")


def services() -> dict[tuple[int, float], int]:
    result: dict[tuple[int, float], int] = {}
    for process in psutil.process_iter():
        try:
            if process.name().startswith(SERVICE_PREFIXES):
                result[(process.pid, process.create_time())] = process.memory_info().rss
        except psutil.NoSuchProcess:
            continue
    return result


def tree_memory(process: psutil.Process) -> dict[tuple[int, float], int]:
    result: dict[tuple[int, float], int] = {}
    for member in [process, *process.children(recursive=True)]:
        try:
            result[(member.pid, member.create_time())] = member.memory_info().rss
        except psutil.NoSuchProcess:
            continue
    return result


def memory_used(baseline: dict[tuple[int, float], int], auxiliary: Path) -> int:
    processes = tree_memory(psutil.Process())
    if auxiliary.exists():
        record = cast(dict[str, float], json.loads(auxiliary.read_bytes()))
        with contextlib.suppress(psutil.NoSuchProcess):
            process = psutil.Process(int(record["pid"]))
            if abs(process.create_time() - record["birth"]) < 0.01:
                processes.update(tree_memory(process))
    for identity, rss in services().items():
        # Existing services can be shared. Charge their growth conservatively;
        # shrinking another VM must not offset this builder's memory usage.
        processes[identity] = max(0, rss - baseline.get(identity, 0))
    return sum(processes.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registry", type=Path)
    parser.add_argument("limits", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    limits = cast(dict[str, int | str], json.loads(args.limits.read_bytes()))
    baseline = services()
    started = time.monotonic()
    state: dict[str, object] = {
        "pid": os.getpid(),
        "birth": psutil.Process().create_time(),
        "peak_memory_bytes": 0,
        "stop_reason": None,
        "vm_name": limits.get("vm_name"),
    }
    with args.registry.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def save() -> None:
            temporary = args.registry.with_suffix(".tmp")
            with temporary.open("w") as stream:
                json.dump(state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(args.registry)

        save()

        def terminated(_signal: int, _frame: object) -> None:
            state["stop_reason"] = "cancelled"

        signal.signal(signal.SIGTERM, terminated)
        signal.signal(signal.SIGINT, terminated)
        child: subprocess.Popen[bytes] | None = None
        code = 125
        try:
            child = subprocess.Popen(args.command, start_new_session=True)
            state.update(child_pid=child.pid, child_birth=psutil.Process(child.pid).create_time())
            save()
            while child.poll() is None:
                if state["stop_reason"]:
                    break
                used = memory_used(baseline, Path(str(limits["auxiliary_registry"])))
                state["peak_memory_bytes"] = max(int(str(state["peak_memory_bytes"])), used)
                if time.monotonic() - started >= int(limits["timeout"]):
                    state["stop_reason"] = "timeout"
                elif used > int(limits["max_host_memory_bytes"]):
                    state["stop_reason"] = "memory"
                elif shutil.disk_usage(str(limits["disk_root"])).free < int(
                    limits["min_free_disk_bytes"]
                ):
                    state["stop_reason"] = "disk"
                elif Path(str(limits["log"])).stat().st_size > 8 * 1024 * 1024:
                    state["stop_reason"] = "output"
                save()
                if state["stop_reason"]:
                    break
                time.sleep(0.5)
            if child.returncode is not None:
                code = child.returncode
        except psutil.Error, OSError, ValueError:
            state["stop_reason"] = "resource_measurement_unavailable"
            raise
        finally:
            if child:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            state["finished"] = True
            save()
        sys.exit(code)


if __name__ == "__main__":
    main()
