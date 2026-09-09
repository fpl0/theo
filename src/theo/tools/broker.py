"""Run-scoped tool grants, argument validation and durable replay receipts.

The broker owns cancellation, audit recording and its Unix-socket transport.
Authorized calls are dispatched through the catalog to capability handlers.
"""

import asyncio
import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from theo.config import Settings
from theo.domain import (
    Json,
    TheoError,
    ToolContext,
    ToolResult,
    digest,
    encode,
)
from theo.observability import telemetry
from theo.storage import Database
from theo.tools.authorization import BoundDatabase, authorize
from theo.tools.contracts import ToolCall
from theo.tools.registry import REGISTRY


class ToolBroker:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self.tokens: dict[str, ToolContext] = {}
        self.calls: dict[str, set[asyncio.Task[Any]]] = {}
        self.server: asyncio.Server | None = None

    def grant(self, context: ToolContext) -> str:
        token = secrets.token_urlsafe(32)
        self.tokens[token] = context
        return token

    def revoke(self, run_id: str) -> None:
        for task in self.calls.pop(run_id, set()):
            task.cancel()
        self.tokens = {
            token: context for token, context in self.tokens.items() if context.run_id != run_id
        }

    def definitions(self, context: ToolContext) -> list[Json]:
        return [
            {
                "name": name,
                "description": definition.description,
                "inputSchema": definition.schema.model_json_schema(),
            }
            for name, definition in REGISTRY.items()
            if name in context.tools
        ]

    async def call(self, token: str, name: str, arguments: Json) -> ToolResult:
        context = self.tokens.get(token)
        link = (
            await self.db.one(
                "SELECT traceparent FROM telemetry_links WHERE kind='run' AND entity_id=?",
                (context.run_id,),
            )
            if context
            else None
        )
        tool = name if name in REGISTRY else "unknown"
        with telemetry.operation(
            "tool.call", upstream=link["traceparent"] if link else "", tool=tool
        ):
            result = await self._call(token, name, arguments)
            telemetry.mark_outcome(result.status)
            telemetry.measure("theo_tools", tool=tool, outcome=result.status)
            telemetry.event("tool.result", tool=tool, outcome=result.status)
            return result

    async def _call(self, token: str, name: str, arguments: Json) -> ToolResult:
        context = self.tokens.get(token)
        if context is None or name not in context.tools or name not in REGISTRY:
            return ToolResult(status="denied", error="Tool grant unavailable")
        db = BoundDatabase(self.db, context)
        current = asyncio.current_task()
        if current:
            self.calls.setdefault(context.run_id, set()).add(current)
        try:
            await db.write(lambda connection: None)
            args = REGISTRY[name].schema.model_validate(arguments).model_dump(exclude_none=True)
            receipt_key = digest({"tool": name, "arguments": args})
            if REGISTRY[name].effect == "write":

                def reserve(connection: sqlite3.Connection) -> Json | None:
                    old = connection.execute(
                        "SELECT result FROM tool_receipts WHERE owner_id=? AND job_id=? AND semantic_key=?",
                        (context.owner_id, context.job_id, receipt_key),
                    ).fetchone()
                    if old:
                        return json.loads(old[0])
                    uncertain = ToolResult(
                        status="uncertain",
                        error="The prior tool attempt has no committed receipt; inspect its effects before a new job retries it.",
                    )
                    connection.execute(
                        "INSERT INTO tool_receipts VALUES(?,?,?,?,?)",
                        (
                            context.owner_id,
                            context.job_id,
                            receipt_key,
                            uncertain.model_dump_json(),
                            db.clock(),
                        ),
                    )
                    return None

                cached = await db.write(reserve)
                if cached is not None:
                    return ToolResult.model_validate(cached)
            result = await self._dispatch(db, context, name, args)
            if REGISTRY[name].effect == "write":
                await db.execute(
                    "UPDATE tool_receipts SET result=? WHERE owner_id=? AND job_id=? AND semantic_key=?",
                    (result.model_dump_json(), context.owner_id, context.job_id, receipt_key),
                )
            await db.message(
                context.owner_id,
                context.conversation_id,
                "tool",
                encode({"tool": name, "result": result.model_dump(mode="json")})[:100000],
                run_id=context.run_id,
                source=f"tool:{name}",
            )
            return result
        except ValidationError:
            return ToolResult(status="invalid", error="Arguments do not match the tool schema")
        except TheoError as exc:
            return ToolResult(status=exc.code, error=str(exc), retryable=exc.retryable)
        except (ValueError, OSError, sqlite3.Error) as exc:
            return ToolResult(
                status="failed",
                error=f"Tool rejected input or could not commit: {type(exc).__name__}",
            )
        finally:
            if current:
                self.calls.get(context.run_id, set()).discard(current)

    async def _dispatch(
        self, db: BoundDatabase, ctx: ToolContext, name: str, args: Json
    ) -> ToolResult:
        scope = await authorize(db, ctx, name, args)
        return await REGISTRY[name].handler(ToolCall(db, self.settings, ctx, scope), args)

    async def listen(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._handle, str(path), limit=1024 * 1024)
        path.chmod(0o600)
        if self.settings.runner_gid is not None:
            import os

            os.chown(path.parent, -1, self.settings.runner_gid)
            os.chown(path, -1, self.settings.runner_gid)
            path.parent.chmod(0o710)
            path.chmod(0o660)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while raw := await asyncio.wait_for(reader.readline(), 60):
                packet: Json = json.loads(raw)
                token = packet.get("token", "")
                context = self.tokens.get(token)
                if context is None:
                    response: Json = {"status": "denied", "error": "Run grant unavailable"}
                elif packet.get("method") == "list":
                    response = {"tools": self.definitions(context)}
                else:
                    response = (
                        await self.call(token, packet.get("name", ""), packet.get("arguments", {}))
                    ).model_dump(mode="json")
                writer.write((encode(response) + "\n").encode())
                await writer.drain()
        except TimeoutError, ValueError, ConnectionError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def close(self) -> None:
        for run_id in list(self.calls):
            self.revoke(run_id)
        self.tokens.clear()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
