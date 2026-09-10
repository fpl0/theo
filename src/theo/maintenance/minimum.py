"""Preserve operator-pinned tests and check configuration across candidate edits.

The additional acceptance tree uses candidate implementation bytes and the
previous recipe's tests, tooling lock and configuration. Candidate tests still run
separately. A proposed test deletion or relaxed config cannot remove this floor.
"""

import stat
from pathlib import Path

from theo.domain import Denied
from theo.maintenance.configuration import require_root_parents
from theo.maintenance.source import files_digest, read_source

CONFIGURATION = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "ruff.toml",
        ".ruff.toml",
        "pyrightconfig.json",
    }
)


def acceptance_files(
    baseline: Path, expected: str, candidate: dict[str, tuple[bytes, int]]
) -> dict[str, tuple[bytes, int]]:
    require_root_parents(baseline)
    for path in (baseline, *baseline.rglob("*")):
        info = path.lstat()
        if (
            info.st_uid != 0
            or info.st_mode & 0o022
            or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
        ):
            raise Denied("Minimum verification source must be a protected root-owned tree")
    previous = read_source(baseline)
    if files_digest(previous) != expected:
        raise Denied("The installed minimum verification source changed")
    if not {"pyproject.toml", "uv.lock", "tests/conftest.py"} <= previous.keys():
        raise Denied("Minimum source lacks its test configuration or dependency lock")
    return overlay(previous, candidate)


def overlay(
    previous: dict[str, tuple[bytes, int]], candidate: dict[str, tuple[bytes, int]]
) -> dict[str, tuple[bytes, int]]:
    """Use the submitted implementation with the pinned recipe's tests and config."""
    return {
        **{
            name: value
            for name, value in candidate.items()
            if not name.startswith("tests/") and name not in CONFIGURATION
        },
        **{
            name: value
            for name, value in previous.items()
            if name.startswith("tests/") or name in CONFIGURATION
        },
    }
