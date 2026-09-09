"""Internal contracts connecting the tool catalog, broker and handlers.

ToolCall carries the already-authorized invocation. ToolDefinition makes input
schema, implementation and receipt policy explicit for each advertised capability.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from theo.config import Settings
from theo.domain import Json, StrictModel, ToolContext, ToolResult
from theo.storage import Database


@dataclass(frozen=True, slots=True)
class ToolCall:
    db: Database
    settings: Settings
    context: ToolContext
    scope: str | None


type Handler = Callable[[ToolCall, Json], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    schema: type[StrictModel]
    description: str
    handler: Handler
    effect: Literal["read", "write", "outbound"] = "write"
