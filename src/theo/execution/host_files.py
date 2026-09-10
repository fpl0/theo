"""Bounded host reads under explicit operator path grants.

No shell, network, writes or permission escalation. Descriptor-based traversal
prevents a symlink swap from escaping the checked path during a read.
"""

import os
import stat
from contextlib import ExitStack
from itertools import islice
from pathlib import Path

from theo.domain import Denied, Json


def read_host_path(
    path: Path,
    roots: tuple[Path, ...],
    protected: tuple[Path, ...],
    *,
    offset: int = 0,
    limit: int = 32768,
    protect_credentials: bool = True,
) -> Json:
    if not path.is_absolute() or not 0 <= offset <= 1_000_000_000 or not 1 <= limit <= 65536:
        raise Denied("Use an absolute path and bounded read page")
    resolved = path.resolve(strict=True)
    # Settings freezes canonical roots when the operator configuration is loaded.
    # Resolving them again would let a replaced grant directory widen authority.
    if not any(resolved.is_relative_to(root) for root in roots):
        raise Denied("Path is outside the operator's host_read_roots")
    if any(resolved.is_relative_to(root.resolve()) for root in protected):
        raise Denied("Protected Theo state and native credentials are not host-readable")
    if protect_credentials and any(
        part in {".credentials", ".ssh", ".aws", ".gnupg"} or part == ".env"
        for part in resolved.parts
    ):
        raise Denied("Credential paths are not host-readable")
    with ExitStack() as cleanup:
        descriptor = os.open(resolved.anchor, os.O_RDONLY | os.O_DIRECTORY)
        cleanup.callback(os.close, descriptor)
        for index, part in enumerate(resolved.parts[1:]):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(resolved.parts) - 2:
                flags |= os.O_DIRECTORY
            descriptor = os.open(part, flags, dir_fd=descriptor)
            cleanup.callback(os.close, descriptor)
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode):
            # Directory cursors are best-effort if the directory changes. The cap
            # bounds scanning as well as the returned page, without sorting it all.
            if offset > 10000:
                raise Denied("Narrow the directory instead of scanning beyond 10000 entries")
            page_size = min(limit, 200)
            with os.scandir(descriptor) as entries:
                page = [entry.name for entry in islice(entries, offset, offset + page_size + 1)]
            return {
                "path": str(resolved),
                "kind": "directory",
                "entries": page[:page_size],
                "next_offset": offset + page_size if len(page) > page_size else None,
            }
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Denied("Only ordinary files with one link can be read")
        os.lseek(descriptor, offset, os.SEEK_SET)
        data = os.read(descriptor, limit)
        if b"\x00" in data:
            raise Denied("Binary files need a text export; this tool reads text only")
        after = os.fstat(descriptor)
        if (info.st_mtime_ns, info.st_size) != (after.st_mtime_ns, after.st_size):
            raise Denied("File changed during the read; retry against a stable source")
        return {
            "path": str(resolved),
            "kind": "file",
            "text": data.decode("utf-8", errors="replace"),
            "offset": offset,
            "bytes": len(data),
            "size": info.st_size,
            "next_offset": offset + len(data) if offset + len(data) < info.st_size else None,
        }
