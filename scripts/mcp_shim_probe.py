"""Drive a real native app against Theo's MCP shim and prove a tool call reached SQLite.

This exercises the shim as the native runtime actually uses it: the app spawns
`python -m theo.mcp_shim`, speaks MCP over stdio, and the shim relays to a real
broker socket. It uses a throwaway data root and grants only `remember`/`recall`,
so the model cannot run commands, send messages or touch an existing assistant.
It does consume one included run on the selected subscription account.
"""

import argparse
import asyncio
import contextlib
import importlib.metadata
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from native_e2e import require_local_live

from theo.backends.process import stop_process
from theo.config import Settings
from theo.domain import ToolContext, uid
from theo.storage import Database
from theo.tools.broker import ToolBroker
from theo.work.jobs import Jobs

GRANTED = frozenset({"remember", "recall"})


async def build_broker(root: Path, socket_path: Path):
    db = Database(root / "data")
    await db.initialize()
    conversation = await db.conversation("owner", "local", "owner")
    broker = ToolBroker(db, Settings(encrypted_storage_verified=True))
    jobs = Jobs(db, "owner")
    job_id = await jobs.enqueue(conversation, "delegated", {"text": "probe"}, "probe")
    job = await jobs.claim("background", "probe-worker")
    run_id = uid()
    await db.execute(
        "INSERT INTO runs(id,owner_id,job_id,generation,backend,model,status,started_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (run_id, "owner", job_id, job["generation"], "probe", "probe", "running", db.clock()),
    )
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
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
    return db, broker, token, workspace


def shim_entry(socket_path: Path, token: str) -> dict[str, object]:
    return {
        "command": sys.executable,
        "args": ["-m", "theo.mcp_shim"],
        "env": {
            "THEO_TOOL_SOCKET": str(socket_path),
            "THEO_TOOL_TOKEN": token,
            "PATH": os.environ.get("PATH", ""),
        },
    }


async def capture(command: list[str], workspace: Path) -> tuple[int | None, str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(workspace),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), 300)
    finally:
        await stop_process(process)
    return process.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def run_claude(socket_path: Path, token: str, workspace: Path, model: str, marker: str):
    prompt = (
        f"Call the tool mcp__theo__remember exactly once with body set to '{marker}'. "
        "Then call mcp__theo__recall with query set to 'probe'. Reply with only the word DONE."
    )
    return await capture(
        [
            "claude",
            "--print",
            "--model",
            model,
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps({"mcpServers": {"theo": shim_entry(socket_path, token)}}),
            "--allowedTools",
            "mcp__theo__remember",
            "mcp__theo__recall",
            "--max-turns",
            "8",
            "--output-format",
            "json",
            prompt,
        ],
        workspace,
    )


async def run_codex(socket_path: Path, token: str, workspace: Path, model: str, marker: str):
    prompt = (
        f"Call the MCP tool theo/remember exactly once with body set to '{marker}'. "
        "Then call theo/recall with query set to 'probe'. Reply with only the word DONE."
    )
    entry = shim_entry(socket_path, token)
    return await capture(
        [
            "codex",
            "exec",
            "--model",
            model,
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ephemeral",
            "--approve-for-me",
            "-c",
            f'mcp_servers.theo.command="{entry["command"]}"',
            "-c",
            'mcp_servers.theo.args=["-m","theo.mcp_shim"]',
            "-c",
            f'mcp_servers.theo.env.THEO_TOOL_SOCKET="{socket_path}"',
            "-c",
            f'mcp_servers.theo.env.THEO_TOOL_TOKEN="{token}"',
            "-c",
            f'mcp_servers.theo.env.PATH="{os.environ.get("PATH", "")}"',
            prompt,
        ],
        workspace,
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--backend", choices=["claude", "codex"], required=True)
    parser.add_argument("--model", required=True, help="Exact included model ID for that account")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    require_local_live(args.live)

    marker = "theo-mcp-probe-" + uuid.uuid4().hex[:12]
    root = Path(tempfile.mkdtemp(prefix="theo-probe-"))
    # An AF_UNIX path must fit ~104 bytes, so the socket gets its own short directory.
    socket_root = Path(tempfile.mkdtemp(prefix="theo-sock-", dir="/tmp"))
    socket_path = socket_root / "t.sock"
    db, broker, token, workspace = await build_broker(root, socket_path)

    async def renew_lease():
        job = await db.one("SELECT id,generation FROM jobs WHERE status='running'")
        assert job
        while True:
            await asyncio.sleep(15)
            await Jobs(db, "owner").heartbeat(job["id"], job["generation"])

    heartbeat = asyncio.create_task(renew_lease())
    try:
        runner = run_claude if args.backend == "claude" else run_codex
        code, out, err = await runner(socket_path, token, workspace, args.model, marker)
        rows = await db.read("SELECT body FROM memory_revisions")
        persisted = any(marker in (row["body"] or "") for row in rows)
        report = {
            "backend": args.backend,
            "model": args.model,
            "marker": marker,
            "exit_code": code,
            "memory_revisions": len(rows),
            "marker_persisted": persisted,
            "mcp_version": importlib.metadata.version("mcp"),
            "stdout_tail": out[-2000:],
            "stderr_tail": err[-2000:],
        }
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 0 if persisted and code == 0 else 1
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await broker.close()
        await db.close()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(socket_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
