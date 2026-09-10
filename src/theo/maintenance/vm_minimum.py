"""Seal an independent minimum test tree and tooling inside the disposable guest.

Candidate packaging has not run when this environment is installed and sealed.
Only this pinned helper can change its source, tests, dependency environment or
configuration; all test execution still uses the unprivileged guest identity.
"""

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import cast

ROOT = Path("/private/var/theo-builder")
SOURCE = ROOT / "minimum-source"
ENVIRONMENT = ROOT / "minimum-environment"
TOOLS = ROOT / "tools"


def manifest() -> dict[str, object]:
    if os.geteuid() != 0 or sys.platform != "darwin":
        raise RuntimeError("Minimum verification setup requires the disposable Mac guest")
    model = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], timeout=5)
    if not model.startswith(b"VirtualMac"):
        raise RuntimeError("Minimum verification setup is forbidden on a physical host")
    return cast(dict[str, object], json.loads((ROOT / "inputs/manifest.json").read_bytes()))


def freeze(root: Path) -> None:
    subprocess.run(["/bin/chmod", "-R", "-P", "-N", str(root)], check=True, timeout=60)
    for path in (root, *root.rglob("*")):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            resolved = path.resolve(strict=True)
            if not any(resolved.is_relative_to(parent) for parent in (root, TOOLS)):
                raise RuntimeError("Minimum environment contains an unexpected symlink")
            os.chown(path, 0, 0, follow_symlinks=False)
            if os.chmod in os.supports_follow_symlinks:
                os.chmod(path, 0o755, follow_symlinks=False)
        elif stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
            os.chown(path, 0, 0)
            path.chmod(0o555 if stat.S_ISDIR(info.st_mode) or info.st_mode & 0o111 else 0o444)
        else:
            raise RuntimeError("Minimum environment contains a special file or hard link")


def prepare() -> None:
    values = manifest()
    if values.get("minimum_sha256") is None:
        return
    for path in (SOURCE, ENVIRONMENT):
        path.mkdir(mode=0o700)
        os.chown(path, 622, 20)
    result = subprocess.run(
        [
            str(TOOLS / "python/bin/python3"),
            "-I",
            "-B",
            "-c",
            "import tarfile; "
            f"t=tarfile.open({str(ROOT / 'inputs/minimum.tar')!r}); "
            f"t.extractall({str(SOURCE)!r},filter='data'); t.close()",
        ],
        user=622,
        group=20,
        extra_groups=[],
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Minimum source extraction failed")
    freeze(SOURCE)
    (SOURCE / ".venv").symlink_to(ENVIRONMENT, target_is_directory=True)


def seal() -> None:
    manifest()
    site = ENVIRONMENT / "lib/python3.14/site-packages"
    if (
        site.is_symlink()
        or not site.is_dir()
        or not site.resolve(strict=True).is_relative_to(ENVIRONMENT)
    ):
        raise RuntimeError("Minimum test environment has no regular package directory")
    projection = site / "_theo_minimum.pth"
    if projection.exists() or projection.is_symlink():
        raise RuntimeError("Minimum source projection is already claimed")
    projection.write_text(str(SOURCE / "src") + "\n")
    freeze(ENVIRONMENT)
    check()


def check() -> dict[str, object]:
    values = manifest()
    expected = cast(dict[str, list[object]], values["minimum_files"])
    actual: dict[str, list[object]] = {}
    for path in SOURCE.rglob("*"):
        if path == SOURCE / ".venv":
            if path.resolve(strict=True) != ENVIRONMENT:
                raise RuntimeError("Minimum interpreter selection changed")
            continue
        info = path.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("Minimum source is writable by the candidate")
        if path.is_dir():
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("Minimum source contains an unexpected file")
        actual[str(path.relative_to(SOURCE))] = [
            hashlib.sha256(path.read_bytes()).hexdigest(),
            0o755 if info.st_mode & 0o111 else 0o644,
        ]
    if actual != expected:
        raise RuntimeError("Minimum tests, configuration or implementation changed")
    for path in (ENVIRONMENT, *ENVIRONMENT.rglob("*")):
        info = path.lstat()
        if info.st_uid != 0 or (not path.is_symlink() and info.st_mode & 0o022):
            raise RuntimeError("Minimum tooling is writable by the candidate")
    return {"minimum_sha256": values["minimum_sha256"], "sealed": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "seal", "check"))
    args = parser.parse_args()
    if args.operation == "prepare":
        prepare()
    elif args.operation == "seal":
        seal()
    else:
        print(json.dumps(check()), flush=True)


if __name__ == "__main__":
    main()
