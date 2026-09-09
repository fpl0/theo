"""Agent Client Protocol adapters for Cursor and Grok.

Translates ACP notifications and stop reasons into Theo events while rejecting
native filesystem, terminal and permission requests outside the Theo broker.
"""

import asyncio
import sys
from typing import Any

from theo.backends.base import Emitter, NativeBackend, classify_error
from theo.backends.policy import (
    Accounts,
)
from theo.backends.process import MAX_FRAME
from theo.config import Settings
from theo.domain import (
    Denied,
    ExecutionOutcome,
    ExecutionRequest,
    Json,
    Outcome,
    ProtocolError,
)
from theo.execution import isolation
from theo.storage import Database


class ACPBackend(NativeBackend):
    def __init__(self, name: str, db: Database, settings: Settings, binary: str | None = None):
        super().__init__(db, settings, binary or ("agent" if name == "cursor" else "grok"))
        self.name = name

    async def execute(self, request: ExecutionRequest, emit: Emitter) -> ExecutionOutcome:
        # Official ACP SDK handles versioned schema validation, request correlation and streaming.
        import acp
        from acp.schema import (
            ClientCapabilities,
            EnvVariable,
            ImageContentBlock,
            Implementation,
            McpServerStdio,
            TextContentBlock,
        )

        env, account = await self.preparation(request)
        texts: list[str] = []
        client = _ACPClient(emit, texts)
        command = [self.binary, "acp"] if self.name == "cursor" else [self.binary, "agent", "stdio"]
        command, options = isolation.launch_options(
            self.settings, self.db.root, request.workspace, command
        )
        self.process = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            cwd=request.workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=MAX_FRAME,
            **options,
        )
        await self.track(request)
        assert self.process.stdin and self.process.stdout
        connection = acp.connect_to_agent(client, self.process.stdin, self.process.stdout)
        try:
            initialized = await connection.initialize(
                protocol_version=1,
                client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="theo", version="0.1.0"),
            )
            if initialized.protocol_version != 1:
                raise ProtocolError("Unqualified ACP protocol version")
            session = await connection.new_session(
                cwd=str(request.workspace),
                mcp_servers=[
                    McpServerStdio(
                        name="theo",
                        command=str(self.settings.worker_python or sys.executable),
                        args=["-m", "theo.mcp_shim"],
                        env=[
                            EnvVariable(name="THEO_TOOL_SOCKET", value=request.tool_socket),
                            EnvVariable(name="THEO_TOOL_TOKEN", value=request.tool_token),
                        ],
                    )
                ],
            )
            model_options = [
                option for option in (session.config_options or []) if option.category == "model"
            ]
            if not model_options:
                raise ProtocolError("ACP runtime did not expose a qualified model selector")
            await connection.set_config_option(
                session_id=session.session_id, config_id=model_options[0].id, value=request.model
            )
            pictures = [
                ImageContentBlock(type="image", data=p["data"], mime_type=p["mime"])
                for p in await self.images(request)
            ]
            result = await connection.prompt(
                session_id=session.session_id,
                prompt=[TextContentBlock(type="text", text=request.context), *pictures],
            )
            if result.stop_reason == "end_turn":
                return ExecutionOutcome(status=Outcome.COMPLETED, text="".join(texts))
            status = (
                Outcome.CANCELLED
                if result.stop_reason == "cancelled"
                else classify_error(str(result.stop_reason))
            )
            if status == Outcome.QUOTA:
                await Accounts(self.db, request.owner_id).exhaust(account)
            return ExecutionOutcome(
                status=status, error=f"Native ACP stop reason: {result.stop_reason}"
            )
        finally:
            await connection.close()
            await self.cancel()


class _ACPClient:
    def __init__(self, emit: Emitter, texts: list[str]):
        self.emit, self.texts = emit, texts

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        packet = update.model_dump(mode="json", by_alias=True)
        kind = packet.get("sessionUpdate")
        if kind == "agent_message_chunk" and packet.get("content", {}).get("type") == "text":
            text = str(packet["content"]["text"])
            self.texts.append(text)
            await self.emit("text_delta", {"text": text})
        elif kind in ("tool_call", "tool_call_update"):
            await self.emit(
                "tool_started" if kind == "tool_call" else "tool_finished",
                {"native_id": packet.get("toolCallId"), "status": packet.get("status")},
            )
        # Thought chunks are deliberately neither retained nor made canonical.

    async def request_permission(self, *args: Any, **kwargs: Any) -> Any:
        from acp.schema import RequestPermissionResponse

        return RequestPermissionResponse.model_validate({"outcome": {"outcome": "cancelled"}})

    def on_connect(self, conn: Any) -> None:
        return None

    async def ext_notification(self, method: str, params: Json) -> None:
        await self.emit("diagnostic", {"vendor_event": method[:80]})

    async def ext_method(self, method: str, params: Json) -> Json:
        raise Denied("ACP extension unavailable")

    async def read_text_file(self, *args: Any, **kwargs: Any) -> Any:
        raise Denied("Use the scoped shared file tool")

    async def write_text_file(self, *args: Any, **kwargs: Any) -> Any:
        raise Denied("Use the scoped shared file tool")

    async def create_terminal(self, *args: Any, **kwargs: Any) -> Any:
        raise Denied("Native terminal capability is disabled")

    async def terminal_output(self, *args: Any, **kwargs: Any) -> Any:
        raise Denied("Native terminal capability is disabled")

    async def release_terminal(self, *args: Any, **kwargs: Any) -> Any:
        raise Denied("Native terminal capability is disabled")

    async def wait_for_terminal_exit(self, *args: Any, **kwargs: Any) -> Any:
        raise Denied("Native terminal capability is disabled")

    async def kill_terminal(self, *args: Any, **kwargs: Any) -> Any:
        raise Denied("Native terminal capability is disabled")

    async def create_elicitation(self, *args: Any, **kwargs: Any) -> Any:
        raise Denied("Native elicitation cannot grant permissions")

    async def complete_elicitation(self, *args: Any, **kwargs: Any) -> None:
        return None
