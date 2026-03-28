"""Memory tools exposed to Claude via Anthropic tool-use.

Seven tools give Claude autonomous control over Theo's memory:

- ``store_memory`` — persist a new observation or fact as a knowledge node.
- ``search_memory`` — search the knowledge graph by semantic similarity.
- ``read_core_memory`` — read the always-on core memory documents.
- ``update_core_memory`` — update a core memory document with changelog.
- ``link_memories`` — create an explicit relationship between two memories.
- ``update_user_model`` — update a structured user model dimension.
- ``advance_onboarding`` — move to the next onboarding phase.

Tool schemas live in :mod:`theo.memory._schemas`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from opentelemetry import trace

from theo.memory import core, edges, nodes, retrieval, user_model
from theo.memory._schemas import TOOL_DEFINITIONS
from theo.memory.auto_edges import record_mention
from theo.onboarding import flow as onboarding_flow

__all__ = ["TOOL_DEFINITIONS", "execute_tool"]

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


# -- Tool execution --------------------------------------------------------


async def execute_tool(
    name: str,
    tool_input: dict[str, object],
    *,
    episode_id: int | None = None,
) -> str:
    """Execute a memory tool and return the result as a string.

    *episode_id*, when provided, enables cross-referencing stored nodes back
    to the episode that triggered the tool call (via ``episode_node``).

    Errors are returned as descriptive strings so Claude can adapt -- they are
    never raised to the caller.
    """
    with tracer.start_as_current_span(
        "execute_tool",
        attributes={"tool.name": name},
    ):
        try:
            # store_memory is special-cased: it needs episode_id to record
            # the mention link, which other tools don't require.
            if name == "store_memory":
                return await _store_memory(tool_input, episode_id=episode_id)
            handler = _TOOL_DISPATCH.get(name)
            if handler is None:
                return f"Unknown tool: {name}"
            return await handler(tool_input)
        except Exception as exc:  # noqa: BLE001
            log.warning("tool execution failed", extra={"tool": name, "error": str(exc)})
            return f"Error executing {name}: {exc}"


# -- Tool implementations -------------------------------------------------


async def _store_memory(
    tool_input: dict[str, object],
    *,
    episode_id: int | None = None,
) -> str:
    kind = str(tool_input.get("kind", "fact"))
    body = str(tool_input.get("body", ""))
    importance = float(str(tool_input.get("importance", 0.5)))

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

    results = await retrieval.hybrid_search(query, limit=limit)
    return json.dumps(
        [
            {
                "id": r.id,
                "kind": r.kind,
                "body": r.body,
                "importance": r.importance,
                "score": round(r.similarity, 4) if r.similarity is not None else None,
            }
            for r in results
        ]
    )


async def _read_core_memory(_tool_input: dict[str, object]) -> str:
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


async def _update_user_model(tool_input: dict[str, object]) -> str:
    framework = str(tool_input.get("framework", ""))
    dimension = str(tool_input.get("dimension", ""))
    raw_value = tool_input.get("value")
    if not isinstance(raw_value, dict):
        return "Error: 'value' must be a JSON object."
    value: dict[str, Any] = cast("dict[str, Any]", raw_value)
    reason = tool_input.get("reason")

    result = await user_model.update_dimension(
        framework,
        dimension,
        value=value,
        reason=str(reason) if reason is not None else None,
    )
    return json.dumps(
        {
            "updated": True,
            "framework": result.framework,
            "dimension": result.dimension,
            "confidence": result.confidence,
            "evidence_count": result.evidence_count,
        }
    )


async def _advance_onboarding(tool_input: dict[str, object]) -> str:
    summary = str(tool_input.get("summary", ""))
    state = await onboarding_flow.get_onboarding_state()
    if state is None:
        return json.dumps({"error": "No active onboarding session."})

    log.info("advancing onboarding", extra={"from_phase": state.phase, "summary": summary})
    new_state = await onboarding_flow.advance_phase(state)
    if new_state is None:
        return json.dumps({"completed": True, "message": "Onboarding complete!"})
    return json.dumps({"phase": new_state.phase, "phase_index": new_state.phase_index})


# -- Dispatch table --------------------------------------------------------

_TOOL_DISPATCH = {
    "search_memory": _search_memory,
    "read_core_memory": _read_core_memory,
    "update_core_memory": _update_core_memory,
    "link_memories": _link_memories,
    "update_user_model": _update_user_model,
    "advance_onboarding": _advance_onboarding,
}
