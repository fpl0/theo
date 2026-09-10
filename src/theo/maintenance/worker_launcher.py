"""Pinned watchdog for a bounded sandboxed verification process group.

Invoked with Python isolated mode before candidate execution. It retains its own
deadline and process receipt when the requesting controller disappears.
"""

import argparse
import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import psutil


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("registry", type=Path)
    parser.add_argument("timeout", type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    with args.registry.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = {"pid": os.getpid(), "birth": psutil.Process().create_time()}

        def save() -> None:
            temporary = args.registry.with_suffix(".tmp")
            with temporary.open("w") as stream:
                json.dump(state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(args.registry)

        save()

        def terminated(_signal: int, _frame: object) -> None:
            raise SystemExit(143)

        signal.signal(signal.SIGTERM, terminated)
        child = subprocess.Popen(args.command, start_new_session=True)
        state["child_pid"] = child.pid
        state["child_birth"] = psutil.Process(child.pid).create_time()
        save()
        try:
            code = child.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            code = 124
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        sys.exit(code)


if __name__ == "__main__":
    main()
