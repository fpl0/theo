"""Read and write scoped text through directory descriptors without following races.

Prepared maintenance trees have a shared group. New files and parent directories
retain that access even when the core service uses a private umask. Other job
trees keep private defaults. Existing foreign-owned files retain their modes.
"""

import os
import stat
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from theo.content.artifacts import scoped_path
from theo.domain import Denied


@contextmanager
def parent(
    workspace: Path, relative: str, *, create: bool = False
) -> Generator[tuple[int, str, bool]]:
    base = workspace.resolve(strict=True)
    target = scoped_path(base, relative)
    parts = target.relative_to(base).parts
    if not parts:
        raise Denied("A workspace text operation requires a file")
    with ExitStack() as stack:
        descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, descriptor)
        info = os.fstat(descriptor)
        shared = bool(info.st_mode & 0o020) and info.st_gid == os.getegid()
        for component in parts[:-1]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            descriptor = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            stack.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if create and shared and info.st_uid == os.geteuid():
                os.fchmod(descriptor, 0o770)
        yield descriptor, parts[-1], shared


def read_text(workspace: Path, relative: str) -> str:
    with parent(workspace, relative) as (directory, name, _):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
                raise Denied("Workspace text must be a bounded regular file without hard links")
            raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise Denied("Workspace text exceeded its read limit")
            return raw.decode()


def write_text(workspace: Path, relative: str, text: str) -> None:
    with parent(workspace, relative, create=True) as (directory, name, shared):
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "wb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Denied("Workspace writes require a regular file without hard links")
            if shared and info.st_uid == os.geteuid():
                os.fchmod(stream.fileno(), 0o660 | (stat.S_IMODE(info.st_mode) & 0o110))
            stream.truncate(0)
            stream.write(text.encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
