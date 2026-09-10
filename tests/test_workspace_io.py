"""Descriptor-relative workspace IO preserves sharing without symlink or hard-link escapes."""

import os
import stat

import pytest

from theo.domain import Denied
from theo.execution.workspace_io import read_text, write_text


@pytest.mark.parametrize("shared", [False, True])
def test_new_workspace_files_keep_the_intended_access_under_a_private_umask(tmp_path, shared):
    workspace = tmp_path / "job"
    workspace.mkdir()
    workspace.chmod(0o770 if shared else 0o700)
    previous = os.umask(0o077)
    try:
        write_text(workspace, "new/package.py", "answer = 1\n")
        write_text(workspace, "new/package.py", "answer = 2\n")
    finally:
        os.umask(previous)
    assert read_text(workspace, "new/package.py") == "answer = 2\n"
    assert stat.S_IMODE((workspace / "new").stat().st_mode) == (0o770 if shared else 0o700)
    assert stat.S_IMODE((workspace / "new/package.py").stat().st_mode) == (
        0o660 if shared else 0o600
    )
    (workspace / "alias.py").symlink_to("new/package.py")
    assert read_text(workspace, "alias.py") == "answer = 2\n"


@pytest.mark.parametrize("operation", [read_text, lambda root, name: write_text(root, name, "bad")])
@pytest.mark.parametrize("redirect", ["parent", "leaf"])
def test_path_replacement_after_resolution_cannot_redirect_io(
    tmp_path, monkeypatch, operation, redirect
):
    workspace = tmp_path / "job"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "sub/value").write_text("scoped")
    private = tmp_path / "private"
    private.mkdir()
    (private / "value").write_text("synthetic private data")
    original = os.open
    changed = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal changed
        if not changed and path == ("sub" if redirect == "parent" else "value"):
            changed = True
            target = workspace / "sub" if redirect == "parent" else workspace / "sub/value"
            target.rename(workspace / "displaced")
            target.symlink_to(private if redirect == "parent" else private / "value")
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(OSError):
        operation(workspace, "sub/value")
    assert changed and (private / "value").read_text() == "synthetic private data"


def test_hard_links_and_special_files_are_rejected_without_truncation(tmp_path):
    workspace = tmp_path / "job"
    workspace.mkdir()
    private = tmp_path / "private"
    private.write_text("synthetic private data")
    os.link(private, workspace / "alias")
    os.mkfifo(workspace / "fifo")
    for name in ("alias", "fifo"):
        with pytest.raises((Denied, OSError)):
            write_text(workspace, name, "bad")
        with pytest.raises(Denied):
            read_text(workspace, name)
    assert private.read_text() == "synthetic private data"
