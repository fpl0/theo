"""Separately pinned privileged host-command launcher with an independent deadline.

Install a root-owned wrapper invoking this file with a root-owned Python in
isolated mode. Only the trusted core identity may invoke that wrapper through
sudo; native and candidate processes retain their inherited OS sandbox. Approval
is owned by the core ledger, never by arguments supplied to this launcher.
"""

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast


def main() -> None:
    if os.geteuid() != 0 or len(sys.argv) != 1:
        raise SystemExit("The pinned host launcher requires its configured privileged identity")
    raw = sys.stdin.buffer.readline(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise SystemExit("Host request exceeds limit")
    request: dict[str, Any] = json.loads(raw)
    raw_argv: object = request["argv"]
    if not isinstance(raw_argv, list):
        raise SystemExit("Invalid privileged host arguments")
    argv = cast(list[Any], raw_argv)
    cwd, timeout = request["cwd"], request["timeout_seconds"]
    if (
        not 1 <= len(argv) <= 100
        or any(not isinstance(part, str) or "\x00" in part for part in argv)
        or not Path(argv[0]).is_absolute()
        or not Path(cwd).is_absolute()
        or not isinstance(timeout, int)
        or not 1 <= timeout <= 600
    ):
        raise SystemExit("Invalid privileged host request")

    def terminated(_signal: int, _frame: object) -> None:
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminated)
    signal.signal(signal.SIGINT, terminated)
    child = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        env={
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": "/var/root",
            "LANG": "en_US.UTF-8",
        },
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + timeout
        while child.poll() is None:
            if time.monotonic() >= deadline:
                code = 124
                break
            readable, _, _ = select.select([sys.stdin.buffer], [], [], 0.1)
            if readable and not os.read(sys.stdin.fileno(), 1):
                # Losing the core's pipe revokes this running command without
                # requiring an unprivileged process to signal a root child.
                code = 143
                break
        else:
            code = child.returncode
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
