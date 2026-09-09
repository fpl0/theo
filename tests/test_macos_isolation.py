"""Real macOS runner checks, using only synthetic files and no model calls."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from theo.execution.isolation import launch_options

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists(),
    reason="Requires the real macOS execution boundary",
)


@pytest.mark.parametrize("generated", [False, True])
async def test_runner_sqlite_works_without_exposing_core_siblings_or_credentials(
    tmp_path, settings, generated
):
    protected = tmp_path / "protected"
    runner = tmp_path / "runner"
    workspace = runner / "workspaces/current"
    sibling = runner / "workspaces/other"
    for directory in (protected, workspace, sibling, runner / ".codex"):
        directory.mkdir(parents=True)
    secret = protected / "secret"
    other = sibling / "secret"
    credential = runner / ".codex/auth.json"
    for path in (secret, other, credential):
        path.write_text("synthetic canary")
    candidate = settings.model_copy(update={"worker_home": runner, "isolation_verified": True})
    code = """
import pathlib, sqlite3, sys
with sqlite3.connect('runtime.sqlite3') as connection:
    connection.execute('CREATE TABLE check_runtime (value TEXT)')
    connection.execute("INSERT INTO check_runtime VALUES ('ok')")
    assert connection.execute('SELECT value FROM check_runtime').fetchone() == ('ok',)
for name in sys.argv[1:]:
    path = pathlib.Path(name)
    for operation in (path.read_text, lambda: path.write_text('changed')):
        try:
            operation()
        except PermissionError:
            continue
        raise AssertionError('Protected canary was accessible')
print('sqlite and boundary checks passed')
"""
    paths = [secret, other, *([credential] if generated else [])]
    command, options = launch_options(
        candidate,
        protected,
        workspace,
        [sys.executable, "-c", code, *map(str, paths)],
        generated=generated,
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=workspace,
        env={"PATH": os.defpath, "HOME": str(runner), "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **options,
    )
    out, err = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode == 0, err.decode(errors="replace")
    assert out.strip() == b"sqlite and boundary checks passed"
    assert all(path.read_text() == "synthetic canary" for path in (secret, other, credential))


async def test_installed_codex_can_start_inside_the_native_boundary(tmp_path, settings):
    executable = shutil.which("codex")
    if executable is None:
        pytest.skip("Codex is not installed; no native startup evidence")
    runner = tmp_path / "runner"
    workspace = runner / "workspaces/probe"
    workspace.mkdir(parents=True)
    candidate = settings.model_copy(update={"worker_home": runner, "isolation_verified": True})
    command, options = launch_options(
        candidate, tmp_path / "protected", workspace, [executable, "--version"]
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=workspace,
        env={"PATH": os.defpath, "HOME": str(runner)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **options,
    )
    out, err = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode == 0, err.decode(errors="replace")
    assert out.startswith(b"codex-cli ")
