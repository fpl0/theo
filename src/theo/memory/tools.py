"""Memory tools exposed to Claude via Anthropic tool-use.

Five tools give Claude autonomous control over Theo's memory:

- ``store_memory`` — persist a new observation or fact as a knowledge node.
- ``search_memory`` — search the knowledge graph by semantic similarity.
- ``read_core_memory`` — read the always-on core memory documents.
- ``update_core_memory`` — update a core memory document with changelog.
- ``link_memories`` — create an explicit relationship between two memories.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from opentelemetry import trace

from theo.memory import core, edges, nodes
from theo.memory.auto_edges import record_mention

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
        "name": "link_memories",
        "description": (
            "Create an explicit relationship between two memories in the knowledge graph. "
            "Use this when you notice two memories are related (e.g. a person and their "
            "project, an event and its location)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_id": {
                    "type": "integer",
                    "description": "ID of the first memory (from search results).",
                },
                "target_id": {
                    "type": "integer",
                    "description": "ID of the second memory.",
                },
                "label": {
                    "type": "string",
                    "description": (
                        "Relationship type (e.g. 'works_on', 'located_in', "
                        "'related_to', 'part_of')."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Why these memories are related.",
                },
            },
            "required": ["source_id", "target_id", "label"],
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


async def execute_tool(  # noqa: PLR0911
    name: str,
    tool_input: dict[str, object],
    *,
    episode_id: int | None = None,
) -> str:
    """Execute a memory tool and return the result as a string.

    *episode_id*, when provided, enables cross-referencing stored nodes back
    to the episode that triggered the tool call (via ``episode_node``).

    Errors are returned as descriptive strings so Claude can adapt — they are
    never raised to the caller.
    """
    with tracer.start_as_current_span(
        "execute_tool",
        attributes={"tool.name": name},
    ):
        try:
            match name:
                case "store_memory":
                    return await _store_memory(tool_input, episode_id=episode_id)
                case "search_memory":
                    return await _search_memory(tool_input)
                case "read_core_memory":
                    return await _read_core_memory()
                case "update_core_memory":
                    return await _update_core_memory(tool_input)
                case "link_memories":
                    return await _link_memories(tool_input)
                case _:
                    return f"Unknown tool: {name}"
        except Exception as exc:  # noqa: BLE001
            log.warning("tool execution failed", extra={"tool": name, "error": str(exc)})
            return f"Error executing {name}: {exc}"


# ── Tool implementations ─────────────────────────────────────────────


async def _store_memory(
    tool_input: dict[str, object],
    *,
    episode_id: int | None = None,
) -> str:
    kind = str(tool_input.get("kind", "fact"))
    body = str(tool_input.get("body", ""))
    raw_importance = tool_input.get("importance", 0.5)
    importance = float(str(raw_importance))

    node_id = await nodes.store_node(
        kind=kind,
        body=body,
        importance=importance,
    )

    if episode_id is not None:
        await record_mention(episode_id, node_id)

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
    body: dict[str, Any] = cast("dict[str, Any]", raw_body)
    reason = tool_input.get("reason")

    new_version = await core.update(
        label,
        body=body,
        reason=str(reason) if reason is not None else None,
    )
    return json.dumps({"updated": True, "label": label, "version": new_version})


async def _link_memories(tool_input: dict[str, object]) -> str:
    raw_source = tool_input.get("source_id")
    raw_target = tool_input.get("target_id")
    if not isinstance(raw_source, int) or not isinstance(raw_target, int):
        return "Error: source_id and target_id must be integers."
    if raw_source <= 0 or raw_target <= 0:
        return "Error: source_id and target_id must be positive."
    source_id = raw_source
    target_id = raw_target
    label = str(tool_input.get("label", "related_to"))
    reason = tool_input.get("reason")

    meta: dict[str, Any] = {"source": "llm_tool"}
    if reason is not None:
        meta["reason"] = str(reason)

    edge_id = await edges.store_edge(
        source_id=source_id,
        target_id=target_id,
        label=label,
        weight=0.8,
        meta=meta,
    )
    return json.dumps({"linked": True, "edge_id": edge_id})
