"""Claude CLI adapter for fresh canonical sessions.

Builds the run-scoped MCP configuration, consumes newline-delimited native events
and translates the final Claude result into Theo execution outcomes.
"""

import asyncio
import json
import sys

from theo.backends.base import TOOL_CONTRACT, Emitter, NativeBackend, classify_error
from theo.backends.policy import (
    Accounts,
)
from theo.backends.process import MAX_FRAME
from theo.domain import (
    ExecutionOutcome,
    ExecutionRequest,
    Json,
    Outcome,
    ProtocolError,
)
from theo.execution import isolation


def claude_terminal(packet: Json, returncode: int) -> ExecutionOutcome:
    if packet.get("type") != "result":
        return ExecutionOutcome(status=Outcome.FAILED, error="Missing native terminal result")
    is_error = (
        packet.get("is_error") is True or packet.get("subtype") != "success" or returncode != 0
    )
    if is_error:
        diagnostic = (
            str(packet.get("subtype", ""))
            + " "
            + str(packet.get("errors", ""))
            + " "
            + str(packet.get("result", ""))
        )
        status = classify_error(diagnostic)
        return ExecutionOutcome(status=status, error=f"Native Claude terminal: {status.value}")
    usage: Json = packet.get("usage") or {}
    return ExecutionOutcome(
        status=Outcome.COMPLETED,
        text=str(packet.get("result", "")),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
    )


class ClaudeBackend(NativeBackend):
    name, binary = "claude", "claude"

    async def execute(self, request: ExecutionRequest, emit: Emitter) -> ExecutionOutcome:
        env, account = await self.preparation(request)
        shim = {
            "mcpServers": {
                "theo": {
                    "command": str(self.settings.worker_python or sys.executable),
                    "args": ["-m", "theo.mcp_shim"],
                    "env": {
                        "THEO_TOOL_SOCKET": request.tool_socket,
                        "THEO_TOOL_TOKEN": request.tool_token,
                    },
                }
            }
        }
        command = [
            self.binary,
            "--print",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--verbose",
            "--model",
            request.model,
            "--max-turns",
            str(request.max_turns),
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps(shim),
            "--allowedTools",
            "mcp__theo__*",
            "--setting-sources",
            "",
            "--settings",
            json.dumps({"autoMemoryEnabled": False}),
            "--system-prompt",
            TOOL_CONTRACT + "\n\n" + request.instructions,
            "--no-session-persistence",
        ]
        command, options = isolation.launch_options(
            self.settings, self.db.root, request.workspace, command
        )
        self.process = await asyncio.create_subprocess_exec(
            *command,
            cwd=request.workspace,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=MAX_FRAME,
            **options,
        )
        await self.track(request)
        assert self.process.stdin and self.process.stdout
        content: list[Json] = [{"type": "text", "text": request.context}]
        for picture in await self.images(request):
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": picture["mime"],
                        "data": picture["data"],
                    },
                }
            )
        packet = {"type": "user", "message": {"role": "user", "content": content}}
        self.process.stdin.write((json.dumps(packet) + "\n").encode())
        await self.process.stdin.drain()
        self.process.stdin.close()
        terminal: Json | None = None
        async for line in self.process.stdout:
            packet: Json = json.loads(line)
            kind = packet.get("type")
            if kind == "result":
                if terminal is not None:
                    raise ProtocolError("Duplicate native terminal result")
                terminal = packet
            elif kind == "system" and packet.get("subtype") == "init":
                if isinstance(packet.get("model"), str):
                    await emit("runtime_metadata", {"model": packet["model"]})
            elif kind == "assistant":
                for block in packet.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        await emit("text_delta", {"text": block.get("text", "")})
                    elif block.get("type") == "tool_use":
                        await emit(
                            "tool_started",
                            {"name": block.get("name"), "native_id": block.get("id")},
                        )
            elif kind not in ("user", "system", "stream_event"):
                await emit("diagnostic", {"vendor_event": str(kind)[:80]})
        returncode = await self.process.wait()
        outcome = claude_terminal(terminal or {}, returncode)
        if outcome.status == Outcome.QUOTA:
            await Accounts(self.db, request.owner_id).exhaust(account)
        return outcome
