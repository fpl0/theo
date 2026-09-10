"""Persist the owned core process identity before exec under a lifetime lock.

The supervisor can recover this identity even if it dies between spawning the
launcher and receiving the subprocess result. No model-facing entry point exists.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path

import psutil


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    args = parser.parse_args()
    if min(args.uid, args.gid) < 0 or (os.geteuid() == 0 and 0 in (args.uid, args.gid)):
        raise ValueError("The core must run under a non-root service identity")
    if os.geteuid() not in (0, args.uid) or (os.geteuid() != 0 and os.getegid() != args.gid):
        raise PermissionError("The launcher cannot assume the requested core identity")
    descriptor = os.open(args.state_root / "core-process.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(descriptor, True)
    state = args.state_root / "core-process.json"
    temporary = state.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(
            {
                "pid": os.getpid(),
                "birth": psutil.Process().create_time(),
                "python": str(args.python),
                "root": str(args.data_root),
            },
            stream,
        )
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(state)
    parent = os.open(state.parent, os.O_RDONLY)
    os.fsync(parent)
    os.close(parent)
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(args.gid)
        os.setuid(args.uid)
    os.execve(
        args.python,
        [str(args.python), "-I", "-B", "-m", "theo", "--data-root", str(args.data_root), "serve"],
        os.environ,
    )


if __name__ == "__main__":
    main()
