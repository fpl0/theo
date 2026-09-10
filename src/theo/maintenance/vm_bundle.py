"""Construct relocatable application prefixes as the unprivileged VM build user.

The controller invokes fixed phases from this protected guest script. It copies
the entire pinned interpreter, including its standard library and dynamic
libraries. Candidate installation and canaries never run as guest root.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path("/private/var/theo-builder")
BUILD = ROOT / "work/bundle-build"
RELOCATED = ROOT / "work/relocated-bundle"

# This is both a POSIX shell launcher and a Python string literal. The script
# locates its own interpreter after either prefix is copied to another path.
LAUNCHER = (
    b"#!/bin/sh\n"
    b'\'\'\'exec\' "$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd)/python" "$0" "$@"\n'
    b"' '''\n"
)


def check_guest() -> None:
    if sys.platform != "darwin" or os.geteuid() != 622:
        raise RuntimeError("Bundle construction requires the unprivileged Mac guest identity")
    model = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], timeout=5)
    if not model.startswith(b"VirtualMac"):
        raise RuntimeError("Bundle guest phases cannot run on a physical host")


def prepare() -> None:
    BUILD.mkdir(mode=0o700)
    with tarfile.open(ROOT / "inputs/python.tar.gz") as archive:
        archive.extractall(BUILD, filter="data")
    (BUILD / "python").rename(BUILD / "core")
    for relative in ("bin/python", "lib/python3.14/os.py"):
        if not (BUILD / "core" / relative).is_file():
            raise RuntimeError("The pinned standalone interpreter prefix is incomplete")


def relocate_scripts(prefix: Path) -> None:
    for path in (prefix / "bin").iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            first = stream.readline(4096)
            if not first.startswith(b"#!") or b"python" not in first.lower():
                continue
            rest = stream.read(1024 * 1024 + 1)
        if len(rest) > 1024 * 1024:
            raise RuntimeError("Interpreter console script exceeds its bound")
        path.write_bytes(LAUNCHER + rest)


def finish() -> None:
    relocate_scripts(BUILD / "core")
    shutil.copytree(BUILD / "core", BUILD / "worker", symlinks=True)
    shutil.copyfile(ROOT / "work/source/uv.lock", BUILD / "uv.lock")
    BUILD.rename(RELOCATED)
    if BUILD.exists():
        raise RuntimeError("The original prefix must be absent for relocation canaries")


def clean() -> None:
    for name in ("core", "worker"):
        prefix = RELOCATED / name
        for path in list(prefix.rglob("__pycache__")):
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        for path in prefix.rglob("*.pyc"):
            path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "finish", "clean"))
    args = parser.parse_args()
    check_guest()
    if args.phase == "prepare":
        prepare()
    elif args.phase == "finish":
        finish()
    else:
        clean()


if __name__ == "__main__":
    main()
