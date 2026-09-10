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
    args = parser.parse_args()
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
    os.execve(
        args.python,
        [str(args.python), "-m", "theo", "--data-root", str(args.data_root), "serve"],
        os.environ,
    )


if __name__ == "__main__":
    main()
