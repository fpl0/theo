"""Immutable bundle descriptors and one atomic application/worker selection.

Manifest verification is independent of candidate code. Rollback selects code;
it never rewinds the canonical database or delivery receipts.
"""

import hashlib
import os
import stat
from pathlib import Path

from pydantic import Field

from theo.domain import Conflict, Denied, StrictModel, digest, uid
from theo.maintenance.contracts import Commit, Identity


class Bundle(StrictModel):
    version: int = Field(default=1, ge=1, le=1)
    bundle_id: Identity
    source_sha: Commit
    tree: Commit
    core_python: str = "core/bin/python"
    worker_python: str = "worker/bin/python"
    schema_min: int = Field(ge=1)
    schema_max: int = Field(ge=1)
    files: dict[str, str]
    verification_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))


def inventory(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.name == "bundle.json" and path.parent == root:
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            if not path.resolve(strict=True).is_relative_to(root.resolve()):
                raise Denied("Bundle contains an external link")
            result[str(path.relative_to(root))] = "link:" + os.readlink(path)
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise Denied("Bundle contains a hard-linked file")
            result[str(path.relative_to(root))] = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                + ":"
                + oct(stat.S_IMODE(info.st_mode))
            )
        elif not stat.S_ISDIR(info.st_mode):
            raise Denied("Bundle contains a special file")
    return result


def verify(root: Path, expected: str | None = None) -> Bundle:
    if root.is_symlink() or not root.is_dir():
        raise Denied("Accepted bundle must be a real directory")
    descriptor = root / "bundle.json"
    if descriptor.is_symlink() or descriptor.stat().st_size > 8 * 1024 * 1024:
        raise Denied("Invalid bundle descriptor")
    bundle = Bundle.model_validate_json(descriptor.read_text())
    if expected is not None and bundle.fingerprint != expected:
        raise Conflict("Bundle descriptor no longer matches acceptance receipt")
    for name in (bundle.core_python, bundle.worker_python):
        path = root / name
        if (
            Path(name).is_absolute()
            or ".." in Path(name).parts
            or not path.resolve().is_relative_to(root.resolve())
            or not path.is_file()
        ):
            raise Denied("Invalid bundle interpreter path")
    if inventory(root) != bundle.files:
        raise Denied("Bundle manifest does not match all installed files and modes")
    return bundle


def atomic_select(pointer: Path, target: Path, expected: Path | None) -> None:
    actual = pointer.resolve() if pointer.is_symlink() else None
    if actual != expected:
        raise Conflict("Active release changed since activation was prepared")
    pointer.parent.mkdir(parents=True, exist_ok=True)
    temporary = pointer.parent / (".current-" + uid())
    temporary.symlink_to(target.resolve(), target_is_directory=True)
    temporary.replace(pointer)
    descriptor = os.open(pointer.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def selected_settings(root: Path) -> tuple[Path, Path] | None:
    pointer = root / "releases/current"
    if not (pointer / "bundle.json").exists():
        return None
    selected = pointer.resolve(strict=True)
    bundle = verify(selected)
    return selected / bundle.core_python, selected / bundle.worker_python
