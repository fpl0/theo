"""Memory tools exposed to Claude via Anthropic tool-use.

Four tools give Claude autonomous control over Theo's memory:

- ``store_memory`` — persist a new observation or fact as a knowledge node.
- ``search_memory`` — search the knowledge graph by semantic similarity.
- ``read_core_memory`` — read the always-on core memory documents.
- ``update_core_memory`` — update a core memory document with changelog.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from opentelemetry import trace

from theo.memory import core, nodes

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# ── Anthropic tool schemas ───────────────────────────────────────────

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "store_memory",
        "description": (
            "Store a new observation, fact, or piece of knowledge in long-term memory. "
            "Use this when the user shares something worth remembering."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": (
                        "Category of the memory (e.g. 'fact', 'preference', 'person', 'event')."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": "The content to remember.",
                },
                "importance": {
                    "type": "number",
                    "description": (
                        "How important this memory is, from 0.0 (trivial) "
                        "to 1.0 (critical). Default 0.5."
                    ),
                },
            },
            "required": ["kind", "body"],
        },
    },
    {
        "name": "search_memory",
        "description": (
            "Search long-term memory for relevant knowledge. "
            "Returns the most semantically similar memories."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return. Default 5.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_core_memory",
        "description": (
            "Read the current state of core memory. Core memory contains four "
            "always-loaded documents: persona, goals, user_model, and context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "update_core_memory",
        "description": (
            "Update one of the four core memory documents (persona, goals, "
            "user_model, context). Use this to evolve your self-model or "
            "understanding of the user over time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "enum": ["persona", "goals", "user_model", "context"],
                    "description": "Which core memory document to update.",
                },
                "body": {
                    "type": "object",
                    "description": "The new document content (replaces existing body).",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief explanation of why this update is being made.",
                },
            },
            "required": ["label", "body"],
        },
    },
]

# ── Tool execution ───────────────────────────────────────────────────


async def execute_tool(name: str, tool_input: dict[str, object]) -> str:
    """Execute a memory tool and return the result as a string.

    Errors are returned as descriptive strings so Claude can adapt — they are
    never raised to the caller.
    """
    with tracer.start_as_current_span(
        "execute_tool",
        attributes={"tool.name": name},
    ):
        try:
            if name == "store_memory":
                return await _store_memory(tool_input)
            if name == "search_memory":
                return await _search_memory(tool_input)
            if name == "read_core_memory":
                return await _read_core_memory()
            if name == "update_core_memory":
                return await _update_core_memory(tool_input)
            return f"Unknown tool: {name}"  # noqa: TRY300
        except Exception as exc:  # noqa: BLE001
            log.warning("tool execution failed", extra={"tool": name, "error": str(exc)})
            return f"Error executing {name}: {exc}"


# ── Tool implementations ─────────────────────────────────────────────


async def _store_memory(tool_input: dict[str, object]) -> str:
    kind = str(tool_input.get("kind", "fact"))
    body = str(tool_input.get("body", ""))
    raw_importance = tool_input.get("importance", 0.5)
    importance = float(str(raw_importance))

    node_id = await nodes.store_node(
        kind=kind,
        body=body,
        importance=importance,
    )
    return json.dumps({"stored": True, "node_id": node_id})


async def _search_memory(tool_input: dict[str, object]) -> str:
    query = str(tool_input.get("query", ""))
    limit = int(str(tool_input.get("limit", 5)))

    results = await nodes.search_nodes(query, limit=limit)
    return json.dumps(
        [
            {
                "id": r.id,
                "kind": r.kind,
                "body": r.body,
                "importance": r.importance,
                "similarity": round(r.similarity, 4) if r.similarity is not None else None,
            }
            for r in results
        ]
    )


async def _read_core_memory() -> str:
    docs = await core.read_all()
    return json.dumps(
        {label: {"body": doc.body, "version": doc.version} for label, doc in docs.items()}
    )


async def _update_core_memory(tool_input: dict[str, object]) -> str:
    label = str(tool_input.get("label", ""))
    raw_body = tool_input.get("body")
    if not isinstance(raw_body, dict):
        return "Error: 'body' must be a JSON object."
    body: dict[str, Any] = dict(raw_body)  # type: ignore[arg-type]
    reason = tool_input.get("reason")

    new_version = await core.update(
        label,
        body=body,
        reason=str(reason) if reason is not None else None,
    )
    return json.dumps({"updated": True, "label": label, "version": new_version})
