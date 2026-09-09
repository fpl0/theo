"""Streaming file checksums used by asset verification and release manifests.

Hashing is independent of the database, model adapters and operator workflows.
"""

import hashlib
from pathlib import Path


def file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
