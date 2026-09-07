"""Credential-free stdio MCP facade. The run grant is the only authority."""

import asyncio
import json
import os
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.stdio import stdio_server


async def main() -> None:
    path, token = os.environ["THEO_TOOL_SOCKET"], os.environ["THEO_TOOL_TOKEN"]

    async def rpc(payload: dict[str, Any]) -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(path, limit=1024 * 1024)
        try:
            writer.write((json.dumps({**payload, "token": token}) + "\n").encode())
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), 90)
            return json.loads(raw)
        finally:
            writer.close()
            await writer.wait_closed()

    async def list_tools(
        _context: ServerRequestContext[Any],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        result = await rpc({"method": "list"})
        return types.ListToolsResult(
            tools=[types.Tool.model_validate(item) for item in result.get("tools", [])]
        )

    async def call_tool(
        _context: ServerRequestContext[Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        result = await rpc(
            {"method": "call", "name": params.name, "arguments": params.arguments or {}}
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
            is_error=result.get("error") is not None,
        )

    server = Server("theo", on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
