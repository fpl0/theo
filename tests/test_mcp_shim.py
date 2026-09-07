import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from theo.domain import ToolContext, uid
from theo.jobs import Jobs
from theo.tools import ToolBroker

GRANTED = frozenset({"remember", "recall"})


def unix_sockets_available(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.bind(str(path))
    except OSError:
        return False
    finally:
        probe.close()
        path.unlink(missing_ok=True)
    return True


@pytest.fixture
async def shim(db, conversation, settings, tmp_path):
    """A live broker socket plus the stdio parameters that launch the shim against it."""
    # An AF_UNIX path must fit ~104 bytes, which pytest's per-test `tmp_path`
    # can exceed once the test name is appended; a short directory always fits.
    socket_root = Path(tempfile.mkdtemp(prefix="theo-mcp-"))
    socket_path = socket_root / "t.sock"
    if not unix_sockets_available(socket_path):
        shutil.rmtree(socket_root, ignore_errors=True)
        pytest.skip("Host denies Unix server sockets; MCP shim integration requires one")
    jobs = Jobs(db, "owner")
    job_id = await jobs.enqueue(conversation, "delegated", {"text": "fixture"}, "fixture")
    job = await jobs.claim("background", "fixture-worker")
    run_id = uid()
    await db.execute(
        "INSERT INTO runs(id,owner_id,job_id,generation,backend,model,status,started_at) VALUES(?,?,?,?,?,?,?,?)",
        (run_id, "owner", job_id, job["generation"], "fixture", "fixture", "running", db.clock()),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = ToolBroker(db, settings)
    token = broker.grant(
        ToolContext(
            owner_id="owner",
            conversation_id=conversation,
            job_id=job_id,
            run_id=run_id,
            generation=job["generation"],
            workspace=workspace,
            tools=GRANTED,
        )
    )
    await broker.listen(socket_path)

    def parameters(environment: dict[str, str] | None = None) -> StdioServerParameters:
        base = dict(os.environ) if environment is None else dict(environment)
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "theo.mcp_shim"],
            env={**base, "THEO_TOOL_SOCKET": str(socket_path), "THEO_TOOL_TOKEN": token},
        )

    try:
        # Each test enters the client itself: an anyio cancel scope must be
        # exited by the task that entered it, which a yielding fixture cannot do.
        yield parameters
    finally:
        await broker.close()
        shutil.rmtree(socket_root, ignore_errors=True)


async def test_shim_lists_only_granted_tools_with_wire_schemas(shim):
    async with Client(shim()) as client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == GRANTED
    remember = next(tool for tool in tools if tool.name == "remember")
    assert remember.description
    assert remember.input_schema["properties"]["body"]["type"] == "string"


async def test_shim_round_trips_a_tool_call_and_reports_failure(shim):
    async with Client(shim()) as client:
        stored = await client.call_tool("remember", {"body": "Theo prefers direct answers."})
        invalid = await client.call_tool("remember", {"body": ""})
    assert stored.is_error is False
    assert json.loads(stored.content[0].text)["status"] == "committed"
    assert invalid.is_error is True
    assert json.loads(invalid.content[0].text)["status"] == "invalid"


async def test_shim_refuses_a_tool_outside_the_run_grant(shim):
    async with Client(shim()) as client:
        result = await client.call_tool("command_run", {"command": "echo hello"})
    assert result.is_error is True
    assert json.loads(result.content[0].text)["status"] == "denied"


async def test_shim_carries_no_database_path_or_channel_credential(shim):
    """`docs/architecture.md` claims the shim holds neither. Take everything else away."""
    scrubbed = {
        "PATH": os.environ.get("PATH", ""),
        # Decoys. The shim reads only its socket and token, so a run that still
        # works here cannot have reached for a data root or a channel secret.
        "THEO_DATA_ROOT": "/nonexistent/decoy-root",
        "THEO_TELEGRAM_TOKEN": "000000:DECOY_MUST_NOT_BE_READ",
        "THEO_HEALTH_TOKEN": "decoy-must-not-be-read",
    }
    async with Client(shim(scrubbed)) as client:
        tools = (await client.list_tools()).tools
        result = await client.call_tool("remember", {"body": "scrubbed environment probe"})
    assert {tool.name for tool in tools} == GRANTED
    assert result.is_error is False
    assert json.loads(result.content[0].text)["status"] == "committed"
