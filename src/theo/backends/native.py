"""Fresh canonical sessions; exactly one terminal outcome per native attempt."""

import asyncio
import base64
import contextlib
import json
import os
import shutil
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from theo.backends.policy import (
    Accounts,
    configuration_files,
    inspect_configuration,
    worker_environment,
)
from theo.backends.process import MAX_FRAME, RpcProcess, stop_process
from theo.config import Settings
from theo.domain import (
    AuthWait,
    Denied,
    ExecutionEvent,
    ExecutionOutcome,
    ExecutionRequest,
    Json,
    Outcome,
    ProtocolError,
    QuotaWait,
    digest,
)
from theo.isolation import launch_options
from theo.storage import Database

type Emitter = Callable[[str, Json], Awaitable[None]]


def classify_error(value: str) -> Outcome:
    lowered = value.lower()
    if any(
        word in lowered
        for word in ("rate_limit", "rate limit", "usage_limit", "quota", "limit reached", "429")
    ):
        return Outcome.QUOTA
    if any(word in lowered for word in ("auth", "login", "sign in", "401", "403")):
        return Outcome.AUTH
    return Outcome.FAILED


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


class NativeBackend:
    name = ""
    binary = ""

    def __init__(self, db: Database, settings: Settings, binary: str | None = None):
        self.db, self.settings = db, settings
        self.binary = binary or self.binary
        self.process: asyncio.subprocess.Process | None = None

    async def version(self) -> str:
        executable = shutil.which(self.binary)
        if not executable:
            raise AuthWait(f"Native {self.name} runtime is not installed")
        process = await asyncio.create_subprocess_exec(
            executable,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            raw, _ = await asyncio.wait_for(process.communicate(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise AuthWait("Runtime version probe timed out") from None
        if process.returncode or len(raw) > 4096:
            raise ProtocolError("Runtime version probe failed")
        return raw.decode(errors="replace").strip()

    async def preparation(self, request: ExecutionRequest) -> tuple[dict[str, str], Json]:
        if os.environ.get("THEO_TEST_OFFLINE") == "1":
            raise Denied("Live native execution is disabled in offline tests")
        home = self.settings.worker_home
        if home is None:
            raise AuthWait("Configure a native runner home and sign in with its official CLI")
        env = worker_environment(home)
        version = await self.version()
        configuration = inspect_configuration(configuration_files(home, self.name))
        fingerprint = digest({"backend": self.name, "version": version, "transport": "theo-v1"})
        account = await Accounts(self.db, request.owner_id).eligible(
            self.name, request.model, fingerprint, configuration
        )
        return env, account

    async def images(self, request: ExecutionRequest) -> list[Json]:
        import io

        from PIL import Image

        from theo.artifacts import Artifacts

        images: list[Json] = []
        artifact_ids = [
            part.artifact_id for part in request.parts if part.kind == "photo" and part.artifact_id
        ]
        artifact_ids.extend(
            str(value)
            for part in request.parts
            for value in part.metadata.get("derived_photos", [])
        )
        for artifact_id in artifact_ids[:8]:
            _, raw = await Artifacts(self.db, self.settings).content(artifact_id)

            def normalize(raw: bytes = raw) -> bytes:
                with Image.open(io.BytesIO(raw)) as picture:
                    picture.thumbnail((1600, 1600))
                    output = io.BytesIO()
                    picture.convert("RGB").save(output, format="JPEG", quality=80)
                    return output.getvalue()

            image = await asyncio.to_thread(normalize)
            images.append(
                {
                    "mime": "image/jpeg",
                    "data": base64.b64encode(image).decode(),
                    "artifact_id": artifact_id,
                }
            )
        return images

    async def execute(self, request: ExecutionRequest, emit: Emitter) -> ExecutionOutcome:
        raise NotImplementedError

    async def events(self, request: ExecutionRequest) -> AsyncIterator[ExecutionEvent]:
        queue: asyncio.Queue[ExecutionEvent | None] = asyncio.Queue(maxsize=512)
        sequence = 0

        async def emit(kind: str, payload: Json) -> None:
            nonlocal sequence
            sequence += 1
            await queue.put(
                ExecutionEvent(run_id=request.run_id, sequence=sequence, kind=kind, payload=payload)
            )

        async def run() -> None:
            try:
                await emit("started", {"backend": self.name})
                async with asyncio.timeout(max(0.01, request.deadline - self.db.clock())):
                    outcome = await self.execute(request, emit)
            except QuotaWait:
                outcome = ExecutionOutcome(
                    status=Outcome.QUOTA, error="Included allowance unavailable"
                )
            except AuthWait as exc:
                outcome = ExecutionOutcome(status=Outcome.AUTH, error=str(exc))
            except asyncio.CancelledError:
                await self.cancel()
                raise
            except TimeoutError:
                await self.cancel()
                outcome = ExecutionOutcome(status=Outcome.INTERRUPTED, error="Run deadline reached")
            except (Denied, ProtocolError, OSError) as exc:
                outcome = ExecutionOutcome(status=Outcome.FAILED, error=str(exc))
            except Exception as exc:
                outcome = ExecutionOutcome(
                    status=Outcome.FAILED, error=f"Native adapter failure: {type(exc).__name__}"
                )
            await emit("terminal", outcome.model_dump(mode="json"))
            await queue.put(None)

        task = asyncio.create_task(run())
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await self.cancel()

    async def track(self, request: ExecutionRequest) -> None:
        if self.process and await self.db.one(
            "SELECT id FROM runs WHERE id=? AND owner_id=?", (request.run_id, request.owner_id)
        ):
            from theo.process_registry import register_worker

            await register_worker(self.db, request.owner_id, request.run_id, self.process.pid)

    async def cancel(self) -> None:
        if self.process:
            await stop_process(self.process)


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
            "--no-session-persistence",
        ]
        command, options = launch_options(self.settings, self.db.root, request.workspace, command)
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


class CodexBackend(NativeBackend):
    name, binary = "codex", "codex"

    async def execute(self, request: ExecutionRequest, emit: Emitter) -> ExecutionOutcome:
        env, account = await self.preparation(request)
        final = asyncio.get_running_loop().create_future()
        texts: list[str] = []
        usage: Json = {}

        async def notify(packet: Json) -> None:
            method, params = packet.get("method"), packet.get("params", {})
            if method == "item/agentMessage/delta":
                text = str(params.get("delta", ""))
                texts.append(text)
                await emit("text_delta", {"text": text})
            elif method == "thread/tokenUsage/updated":
                usage.update(params.get("tokenUsage", {}).get("last", {}))
            elif method == "turn/completed" and not final.done():
                final.set_result(params.get("turn", {}))
            elif method == "theo/transport_error" and not final.done():
                final.set_exception(ProtocolError("Codex transport closed before terminal outcome"))
            elif method == "error" and not final.done():
                final.set_result({"status": "failed", "error": params.get("error", {})})

        async def request_handler(packet: Json) -> Json:
            # Native approval callbacks cannot elevate grants. Shared MCP tools mediate effects.
            method = str(packet.get("method", ""))
            if "requestApproval" in method:
                return {"decision": "decline"}
            raise Denied("Unsupported native request")

        command, options = launch_options(
            self.settings,
            self.db.root,
            request.workspace,
            [self.binary, "app-server", "--listen", "stdio://"],
        )
        async with RpcProcess(
            command, request.workspace, env, notify, request_handler, options
        ) as rpc:
            self.process = rpc.process
            await self.track(request)
            await rpc.call("initialize", {"clientInfo": {"name": "theo", "version": "0.1.0"}})
            await rpc.send({"method": "initialized", "params": {}})
            native_account = await rpc.call("account/read", {"refreshToken": False})
            native_identity: Json = native_account.get("account") or {}
            if native_identity.get("type") != "chatgpt":
                raise AuthWait("Codex must be signed in with ChatGPT subscription authentication")
            thread = await rpc.call(
                "thread/start",
                {
                    "model": request.model,
                    "cwd": str(request.workspace),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": True,
                    "config": {
                        "model_provider": "openai",
                        "mcp_servers": {
                            "theo": {
                                "command": str(self.settings.worker_python or sys.executable),
                                "args": ["-m", "theo.mcp_shim"],
                                "env": {
                                    "THEO_TOOL_SOCKET": request.tool_socket,
                                    "THEO_TOOL_TOKEN": request.tool_token,
                                },
                                "required": True,
                            }
                        },
                    },
                },
            )
            thread_id = thread["thread"]["id"]
            inputs: list[Json] = [{"type": "text", "text": request.context}]
            for picture in await self.images(request):
                path = request.workspace / ("input-" + picture["artifact_id"] + ".jpg")
                path.write_bytes(base64.b64decode(picture["data"]))
                inputs.append({"type": "localImage", "path": str(path)})
            await rpc.call(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": inputs,
                    "model": request.model,
                },
            )
            terminal: Json = await final
            status = terminal.get("status")
            if status == "completed":
                return ExecutionOutcome(
                    status=Outcome.COMPLETED,
                    text="".join(texts),
                    input_tokens=usage.get("inputTokens"),
                    output_tokens=usage.get("outputTokens"),
                )
            outcome = classify_error(json.dumps(terminal.get("error", {})))
            if status == "interrupted":
                outcome = Outcome.INTERRUPTED
            if outcome == Outcome.QUOTA:
                await Accounts(self.db, request.owner_id).exhaust(account)
            return ExecutionOutcome(status=outcome, error=f"Native Codex terminal: {outcome.value}")


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
        command, options = launch_options(self.settings, self.db.root, request.workspace, command)
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
