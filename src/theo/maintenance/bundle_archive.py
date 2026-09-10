"""Extract a bounded guest bundle without executing or following candidate links.

Regular files are written exclusively before any symlink is created. A symlink
cannot be an ancestor of another member, and every link must resolve within the
new output tree. Candidate archive ownership and privileged mode bits are rejected.
"""

import gzip
import os
import shutil
import stat
import tarfile
from collections.abc import Buffer
from pathlib import Path, PurePosixPath

from theo.domain import Denied

# Two complete development prefixes exceed 60,000 entries with the current lock.
# Byte and metadata budgets remain independent of this bounded entry allowance.
MAX_ENTRIES = 100000
MAX_UNPACKED = 4_000_000_000
MAX_FILE = 1_000_000_000
MAX_EXPANDED = MAX_UNPACKED + 128 * 1024 * 1024


class BoundedArchive:
    """Bound metadata reads before tarfile allocates an attacker-declared body.

    Regular members are copied in small chunks. PAX and GNU metadata are parsed
    by tarfile before member validation, so their requested read size and every
    uncompressed seek need an independent bound too.
    """

    def __init__(self, stream: gzip.GzipFile):
        self.stream = stream
        self.parsing = True
        self.record_bytes = 0
        self.metadata_bytes = 0

    def read(self, size: int = -1) -> bytes:
        if not 0 <= size <= 4 * 1024 * 1024 or self.tell() + size > MAX_EXPANDED:
            raise Denied("Bundle archive metadata or expanded stream exceeds its bound")
        if self.parsing and (
            self.record_bytes + size > 1024 * 1024 or self.metadata_bytes + size > 64 * 1024 * 1024
        ):
            raise Denied("Bundle archive metadata exceeds its parse budget")
        body = self.stream.read(size)
        if self.parsing:
            self.record_bytes += len(body)
            self.metadata_bytes += len(body)
        return body

    def tell(self) -> int:
        return self.stream.tell()

    def close(self) -> None:
        self.stream.close()

    def write(self, body: Buffer) -> int:
        raise Denied("The accepted guest archive is read-only")

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence not in (0, 1):
            raise Denied("Unsupported bundle archive seek")
        position = offset + (self.tell() if whence else 0)
        if not 0 <= position <= MAX_EXPANDED:
            raise Denied("Bundle archive expanded stream exceeds its bound")
        return self.stream.seek(position)


def member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or len(name) > 4096
        or path.is_absolute()
        or str(path) != name
        or ".." in path.parts
        or any(ord(char) < 32 for char in name)
    ):
        raise Denied("Bundle archive contains an unsafe member path")
    return path


def extract(archive_path: Path, target: Path) -> None:
    """Create a new private output directory; retain failed output for inspection."""
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    with gzip.GzipFile(archive_path, "rb") as stream:
        _extract(BoundedArchive(stream), target)


def _extract(stream: BoundedArchive, target: Path) -> None:
    with tarfile.open(fileobj=stream, mode="r:") as archive:
        members: dict[PurePosixPath, tarfile.TarInfo] = {}
        total = 0
        for member in archive:
            stream.record_bytes = 0
            path = member_path(member.name)
            if path in members or len(members) >= MAX_ENTRIES:
                raise Denied("Duplicate or excessive bundle archive entries")
            if (
                not (member.isdir() or member.isfile() or member.issym())
                or member.mode & 0o7000
                or not 0 <= member.size <= MAX_FILE
            ):
                raise Denied("Bundle archive contains an unsupported file or mode")
            if member.issym():
                link = PurePosixPath(member.linkname)
                if (
                    link.is_absolute()
                    or not member.linkname
                    or len(member.linkname) > 4096
                    or any(ord(char) < 32 for char in member.linkname)
                ):
                    raise Denied("Bundle archive links must remain relative")
            total += member.size
            if total > MAX_UNPACKED:
                raise Denied("Bundle archive exceeds its unpacked size limit")
            members[path] = member
        stream.parsing = False
        for path in members:
            if any(parent in members and not members[parent].isdir() for parent in path.parents):
                raise Denied("An archive file or link cannot contain another member")
        for path, member in members.items():
            destination = target / str(path)
            if member.isdir():
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
            elif member.isfile():
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                body = archive.extractfile(member)
                if body is None:
                    raise Denied("Missing bundle file body")
                with body, destination.open("xb") as output:
                    shutil.copyfileobj(body, output, 1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                if destination.stat().st_size != member.size:
                    raise Denied("Truncated bundle file body")
                destination.chmod(member.mode & 0o755)
        for path, member in members.items():
            if member.issym():
                destination = target / str(path)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                destination.symlink_to(member.linkname)
                if os.chmod in os.supports_follow_symlinks:
                    os.chmod(destination, 0o755, follow_symlinks=False)
        for path, member in members.items():
            if member.issym():
                destination = target / str(path)
                try:
                    resolved = destination.resolve(strict=True)
                except OSError, RuntimeError:
                    raise Denied("Bundle archive contains a broken or cyclic link") from None
                if not resolved.is_relative_to(target.resolve()):
                    raise Denied("Bundle archive link escapes its output directory")
            elif member.isfile() and not stat.S_ISREG((target / str(path)).lstat().st_mode):
                raise Denied("Bundle archive file changed during extraction")
