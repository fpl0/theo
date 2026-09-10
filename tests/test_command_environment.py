"""Prove generated commands have writable scratch without native credential access."""

import json
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from theo.config import Settings
from theo.execution.isolation import verify_isolation
from theo.execution.workspaces import execute_scoped

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Mac generated-command boundary")


@pytest.fixture(autouse=True)
def isolated_command_environment(monkeypatch):
    # The invoking terminal may customize Node startup. This fixture runs
    # isolated Python commands; the actual Mac sandbox enforces network denial.
    monkeypatch.delenv("NODE_OPTIONS", raising=False)


async def command_settings(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    home = tmp_path / "runner"
    workspace = home / "workspaces/job"
    workspace.mkdir(parents=True)
    (home / ".codex").mkdir()
    (home / ".codex/auth.json").write_text("synthetic private fixture")
    settings = Settings(worker_home=home, worker_python=Path(sys.executable))
    assert (await verify_isolation(settings, core))["verified"]
    return settings.model_copy(update={"isolation_verified": True}), core, workspace


async def test_command_uses_editable_python_and_writable_private_scratch(tmp_path):
    settings, core, workspace = await command_settings(tmp_path)
    binary = workspace / ".venv/bin"
    binary.mkdir(parents=True)
    (binary / "job-python").symlink_to(sys.executable)
    code = (
        "import json,os,pathlib,sys,tempfile\n"
        "paths={key:pathlib.Path(os.environ[key]) for key in ('HOME','TMPDIR','XDG_CACHE_HOME')}\n"
        "for path in paths.values():\n"
        " assert path.is_relative_to(pathlib.Path.cwd())\n"
        " (path/'writable').write_text('synthetic')\n"
        "with tempfile.TemporaryFile() as stream: stream.write(b'synthetic')\n"
        "private=pathlib.Path(sys.argv[1])\n"
        "try: private.read_text()\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('Native credentials became readable')\n"
        "assert os.environ['THEO_TEST_OFFLINE']=='1'\n"
        "print(json.dumps({'scratch_writable':True,'native_credentials_denied':True,'argument':sys.argv[2]}))"
    )
    result = await execute_scoped(
        settings,
        core,
        workspace,
        [
            "job-python",
            "-I",
            "-c",
            code,
            str(settings.worker_home / ".codex/auth.json"),
            "$(literal)",
        ],
    )
    assert result["exit_code"] == 0, result
    assert json.loads(result["output"]) == {
        "scratch_writable": True,
        "native_credentials_denied": True,
        "argument": "$(literal)",
    }


async def test_scratch_symlink_cannot_redirect_core_writes(tmp_path):
    settings, core, workspace = await command_settings(tmp_path)
    (workspace / ".theo").mkdir()
    (workspace / ".theo/command").symlink_to(core, target_is_directory=True)
    result = await execute_scoped(settings, core, workspace, ["/usr/bin/true"])
    assert result["exit_code"] != 0
    assert not (core / "home").exists()
    assert not (core / "tmp").exists()


async def test_command_unix_ipc_is_confined_to_its_own_workspace(tmp_path):
    settings, core, workspace = await command_settings(tmp_path)
    with tempfile.TemporaryDirectory(prefix="theo-ipc-", dir="/tmp") as temporary:
        external = str(Path(temporary) / "outside.sock")
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(external)
            server.listen()
            code = (
                "import errno,socket,sys\n"
                "server=socket.socket(socket.AF_UNIX);server.bind('ipc.sock');server.listen()\n"
                "client=socket.socket(socket.AF_UNIX);client.connect('ipc.sock')\n"
                "peer,_=server.accept();client.sendall(b'ok');assert peer.recv(2)==b'ok'\n"
                "for family,address in ((socket.AF_UNIX,sys.argv[1]),"
                "(socket.AF_INET,('127.0.0.1',9)),(socket.AF_INET6,('::1',9))):\n"
                " with socket.socket(family) as stream:\n"
                "  assert stream.connect_ex(address) in (errno.EPERM,errno.EACCES)\n"
                "print('workspace IPC passed; external Unix, IPv4 and IPv6 denied')"
            )
            result = await execute_scoped(
                settings, core, workspace, [sys.executable, "-I", "-c", code, external]
            )
    assert result["exit_code"] == 0, result
    assert "workspace IPC passed" in result["output"]
