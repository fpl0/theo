"""Exercise editable VM environment handoff without running installation hooks."""

import stat
import subprocess
import sys

import pytest

from theo.domain import Denied
from theo.maintenance.vm_development import editable


def test_development_prefix_imports_current_workspace_edits_and_shares_only_with_its_group(
    tmp_path,
):
    workspace = tmp_path / "coding workspace"
    source = workspace / "src"
    source.mkdir(parents=True)
    probe = source / "vm_editable_probe.py"
    probe.write_text("answer = 2\n")
    prefix = workspace / ".theo/environment"
    package = prefix / "lib/python3.14/site-packages/theo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("answer = 1\n")
    executable = prefix / "bin/python"
    executable.parent.mkdir()
    executable.write_text("synthetic executable")
    executable.chmod(0o555)
    editable(prefix, workspace)
    assert not package.exists()
    assert (package.parent / "_theo_workspace.pth").read_text() == str(source) + "\n"
    script = (
        "import site,sys;site.addsitedir(sys.argv[1]);"
        "import vm_editable_probe;print(vm_editable_probe.answer)"
    )
    for value in (2, 3):
        probe.write_text(f"answer = {value}\n")
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(package.parent)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(value)
    assert stat.S_IMODE(prefix.stat().st_mode) == 0o770
    assert stat.S_IMODE(executable.stat().st_mode) == 0o770
    assert stat.S_IMODE((package.parent / "_theo_workspace.pth").stat().st_mode) == 0o660


@pytest.mark.parametrize("attack", ["newline", "source-link", "package-link"])
def test_development_projection_refuses_paths_that_could_redirect_or_execute(tmp_path, attack):
    workspace = tmp_path / ("coding\nimport os" if attack == "newline" else "coding")
    workspace.mkdir()
    source = workspace / "src"
    if attack == "source-link":
        source.symlink_to(tmp_path, target_is_directory=True)
    else:
        source.mkdir()
    prefix = workspace / ".theo/environment"
    package = prefix / "lib/python3.14/site-packages/theo"
    package.parent.mkdir(parents=True)
    if attack == "package-link":
        package.symlink_to(tmp_path, target_is_directory=True)
    else:
        package.mkdir()
    with pytest.raises(Denied):
        editable(prefix, workspace)
    assert not (package.parent / "_theo_workspace.pth").exists()
