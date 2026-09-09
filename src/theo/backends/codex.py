"""Codex app-server adapter for fresh canonical sessions.

Owns the JSON-RPC initialization, turn protocol, tool approval policy and terminal
result translation; account eligibility and lifecycle live in base.
"""

import asyncio
import base64
import json
import sys
from typing import cast

from theo.backends.base import TOOL_CONTRACT, Emitter, NativeBackend, classify_error
from theo.backends.policy import (
    Accounts,
)
from theo.backends.process import RpcProcess
from theo.domain import (
    AuthWait,
    Denied,
    ExecutionOutcome,
    ExecutionRequest,
    Json,
    Outcome,
    ProtocolError,
)
from theo.execution import isolation
from theo.observability import telemetry


class CodexBackend(NativeBackend):
    name, binary = "codex", "codex"

    async def execute(self, request: ExecutionRequest, emit: Emitter) -> ExecutionOutcome:
        from theo.tools.registry import REGISTRY

        env, account = await self.preparation(request)
        final = asyncio.get_running_loop().create_future()
        texts: list[str] = []
        final_texts: dict[str, str] = {}
        usage: Json = {}

        async def notify(packet: Json) -> None:
            method, params = packet.get("method"), packet.get("params", {})
            if method == "item/agentMessage/delta":
                text = str(params.get("delta", ""))
                texts.append(text)
                await emit("text_delta", {"text": text})
            elif method == "thread/tokenUsage/updated":
                usage.update(params.get("tokenUsage", {}).get("last", {}))
                telemetry.measure(
                    "theo_codex_usage_observed_timestamp", self.db.clock(), gauge=True
                )
                if isinstance(usage.get("cachedInputTokens"), int):
                    telemetry.measure(
                        "theo_codex_cached_input_tokens", usage["cachedInputTokens"], gauge=True
                    )
            elif method == "account/rateLimits/updated":
                limits = params.get("rateLimits", {})
                for window in ("primary", "secondary"):
                    value = limits.get(window)
                    if not isinstance(value, dict):
                        continue
                    value = cast(Json, value)
                    if isinstance(value.get("usedPercent"), (int, float)):
                        telemetry.measure(
                            "theo_codex_allowance_used_ratio",
                            value["usedPercent"] / 100,
                            gauge=True,
                            window=window,
                        )
                        telemetry.measure(
                            "theo_codex_allowance_observed_timestamp",
                            self.db.clock(),
                            gauge=True,
                            window=window,
                        )
                        if isinstance(value.get("resetsAt"), (int, float)):
                            telemetry.measure(
                                "theo_codex_allowance_reset_timestamp",
                                value["resetsAt"],
                                gauge=True,
                                window=window,
                            )

            elif method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                    final_texts[str(item["id"])] = str(item.get("text", ""))
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

        command, options = isolation.launch_options(
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
            with telemetry.operation("codex.connect", backend="codex"):
                await rpc.call("initialize", {"clientInfo": {"name": "theo", "version": "0.1.0"}})
                await rpc.send({"method": "initialized", "params": {}})
                native_account = await rpc.call("account/read", {"refreshToken": False})
            native_identity: Json = native_account.get("account") or {}
            if native_identity.get("type") != "chatgpt":
                telemetry.event("codex.auth.failed", backend="codex", outcome="failed")
                raise AuthWait("Codex must be signed in with ChatGPT subscription authentication")
            telemetry.event(
                "codex.auth.confirmed", backend="codex", kind="chatgpt", model=request.model
            )
            thread = await rpc.call(
                "thread/start",
                {
                    "model": request.model,
                    "cwd": str(request.workspace),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": True,
                    "developerInstructions": TOOL_CONTRACT + "\n\n" + request.instructions,
                    "config": {
                        "model_provider": "openai",
                        "web_search": "disabled",
                        # Match Claude's --tools '' boundary: native persistence and
                        # effects bypass Theo's leases, durable receipts and job queue.
                        "features": {
                            name: False
                            for name in (
                                "multi_agent",
                                "multi_agent_v2",
                                "goals",
                                "memories",
                                "in_app_local_automation",
                                "shell_tool",
                                "unified_exec",
                                "apps",
                                "plugins",
                                "hooks",
                                "skill_mcp_dependency_install",
                                "browser_use",
                                "browser_use_external",
                                "computer_use",
                                "image_generation",
                            )
                        },
                        "mcp_servers": {
                            "theo": {
                                "command": str(self.settings.worker_python or sys.executable),
                                "args": ["-m", "theo.mcp_shim"],
                                "env": {
                                    "THEO_TOOL_SOCKET": request.tool_socket,
                                    "THEO_TOOL_TOKEN": request.tool_token,
                                },
                                "required": True,
                                # Only the host-owned broker mediates these capabilities;
                                # its run grants and durable approvals still apply.
                                "tools": {name: {"approval_mode": "approve"} for name in REGISTRY},
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
                    text="\n".join(final_texts.values()) if final_texts else "".join(texts),
                    input_tokens=usage.get("inputTokens"),
                    output_tokens=usage.get("outputTokens"),
                )
            outcome = classify_error(json.dumps(terminal.get("error", {})))
            if status == "interrupted":
                outcome = Outcome.INTERRUPTED
            if outcome == Outcome.QUOTA:
                await Accounts(self.db, request.owner_id).exhaust(account)
            return ExecutionOutcome(status=outcome, error=f"Native Codex terminal: {outcome.value}")
