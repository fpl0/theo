"""Prepared job permissions must not depend on the controller's primary group."""

import os
import stat

import pytest

from theo.domain import Denied
from theo.maintenance.workspace_access import handoff


@pytest.mark.parametrize("writable", [True, False])
def test_handoff_sets_exact_shared_permissions_and_keeps_internal_links(tmp_path, writable):
    workspace = tmp_path / "job"
    workspace.mkdir()
    source = workspace / "source.py"
    source.write_text("answer = 3\n")
    source.chmod(0o600)
    command = workspace / "check"
    command.write_text("#!/bin/sh\nexit 0\n")
    command.chmod(0o700)
    link = workspace / "current.py"
    link.symlink_to("source.py")
    handoff(workspace, os.getegid(), writable=writable)
    assert source.read_text() == link.read_text() == "answer = 3\n"
    for path in (workspace, source, command, link):
        assert path.lstat().st_uid == os.geteuid()
        assert path.lstat().st_gid == os.getegid()
    assert stat.S_IMODE(source.stat().st_mode) == (0o660 if writable else 0o640)
    assert stat.S_IMODE(command.stat().st_mode) == (0o770 if writable else 0o750)
    assert stat.S_IMODE(workspace.stat().st_mode) == (0o770 if writable else 0o750)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_handoff_rejects_unsafe_tree_before_changing_any_permissions(tmp_path, kind):
    workspace = tmp_path / "job"
    workspace.mkdir()
    first = workspace / "first.py"
    first.write_text("answer = 3\n")
    first.chmod(0o600)
    outside = tmp_path / "private"
    outside.write_text("synthetic private fixture")
    outside.chmod(0o600)
    unsafe = workspace / "unsafe"
    if kind == "symlink":
        unsafe.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, unsafe)
    else:
        os.mkfifo(unsafe)
    with pytest.raises(Denied):
        handoff(workspace, os.getegid(), writable=True)
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert stat.S_IMODE(outside.stat().st_mode) == 0o600
