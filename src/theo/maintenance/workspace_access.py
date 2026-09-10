"""Hand a prepared job tree to its shared group before model execution is admitted.

The controller keeps ownership. Coding jobs can edit their tree; review jobs can
only read it. Group ownership is explicit and independent of service umasks or
the controller's private primary group. This operation never runs on an active job.
"""

import os
import stat
from pathlib import Path

from theo.domain import Denied


def handoff(workspace: Path, group: int, *, writable: bool) -> None:
    if workspace.is_symlink() or not workspace.is_dir():
        raise Denied("Job handoff requires a real controller-owned directory")
    if os.geteuid() != 0 and group not in {os.getegid(), *os.getgroups()}:
        raise Denied("Controller identity is missing the configured workspace group")
    paths = (workspace, *workspace.rglob("*"))
    for path in paths:
        info = path.lstat()
        if info.st_uid != os.geteuid():
            raise Denied("Prepared workspace contains files owned by another identity")
        if stat.S_ISLNK(info.st_mode):
            if not path.resolve(strict=True).is_relative_to(workspace.resolve()):
                raise Denied("Prepared workspace contains an external symlink")
        elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise Denied("Prepared workspace contains a special file")
        elif stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise Denied("Prepared workspace contains a hard link")
    for path in paths:
        mode = path.lstat().st_mode
        os.chown(path, -1, group, follow_symlinks=False)
        if stat.S_ISDIR(mode):
            # The controller retains owner write access for retirement and repair.
            path.chmod(0o770 if writable else 0o750)
        elif stat.S_ISREG(mode):
            path.chmod(
                (0o770 if writable else 0o750) if mode & 0o111 else (0o660 if writable else 0o640)
            )
        elif os.chmod in os.supports_follow_symlinks:
            os.chmod(path, 0o755, follow_symlinks=False)
