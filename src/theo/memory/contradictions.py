"""Contradiction detection between knowledge graph nodes."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from theo.db import db
from theo.llm import TextDelta, stream_response
from theo.memory.nodes import search_nodes

if TYPE_CHECKING:
    from anthropic.types import MessageParam

    from theo.memory._types import NodeResult

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_UPDATE_CONFIDENCE = """
UPDATE node
SET confidence = GREATEST($2, 0.1)
WHERE id = $1
"""

_EXPIRE_ACTIVE_EDGE = """
UPDATE edge
SET valid_to = now()
WHERE source_id = $1
    AND target_id = $2
    AND label = $3
    AND valid_to IS NULL
"""

_INSERT_EDGE = """
INSERT INTO edge (source_id, target_id, label, weight, meta)
VALUES ($1, $2, $3, $4, $5)
RETURNING id
"""

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

_SIMILARITY_THRESHOLD = 0.7
_CONFIDENCE_REDUCTION = 0.3
_CONTRADICTION_MAX_TOKENS = 256


@dataclass(frozen=True, slots=True)
class ConflictResult:
    """A detected contradiction between two nodes."""

    conflicting_node_id: int
    confidence_reduction: float
    explanation: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def check_contradiction(body: str, kind: str) -> ConflictResult | None:
    """Check whether *body* contradicts an existing node of the same *kind*.

    Searches for semantically similar nodes and asks the LLM to judge
    whether the statements are contradictory.  Returns a `ConflictResult`
    for the first contradiction found, or ``None``.
    """
    with tracer.start_as_current_span("check_contradiction") as span:
        candidates = await search_nodes(body, kind=kind, limit=5)
        high_sim = [
            c
            for c in candidates
            if c.similarity is not None and c.similarity > _SIMILARITY_THRESHOLD
        ]

        span.set_attribute("contradiction.candidates_checked", len(high_sim))

        for candidate in high_sim:
            is_conflict, explanation = await _ask_llm_contradiction(body, candidate)
            if is_conflict:
                span.set_attribute("contradiction.found", value=True)
                return ConflictResult(
                    conflicting_node_id=candidate.id,
                    confidence_reduction=_CONFIDENCE_REDUCTION,
                    explanation=explanation,
                )

        span.set_attribute("contradiction.found", value=False)
        return None


async def resolve_contradiction(new_node_id: int, conflict: ConflictResult) -> None:
    """Reduce confidence on both nodes and create a ``contradicts`` edge.

    All three writes (two confidence updates + edge insert) run in a single
    transaction so they commit or roll back atomically.
    """
    with tracer.start_as_current_span(
        "resolve_contradiction",
        attributes={
            "contradiction.new_node_id": new_node_id,
            "contradiction.conflicting_node_id": conflict.conflicting_node_id,
        },
    ):
        async with db.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                _UPDATE_CONFIDENCE,
                new_node_id,
                await _current_confidence(conn, new_node_id) - conflict.confidence_reduction,
            )
            await conn.execute(
                _UPDATE_CONFIDENCE,
                conflict.conflicting_node_id,
                await _current_confidence(conn, conflict.conflicting_node_id)
                - conflict.confidence_reduction,
            )
            await conn.execute(
                _EXPIRE_ACTIVE_EDGE,
                new_node_id,
                conflict.conflicting_node_id,
                "contradicts",
            )
            await conn.fetchval(
                _INSERT_EDGE,
                new_node_id,
                conflict.conflicting_node_id,
                "contradicts",
                1.0,
                {"explanation": conflict.explanation},
            )

        log.info(
            "resolved contradiction",
            extra={
                "new_node_id": new_node_id,
                "conflicting_node_id": conflict.conflicting_node_id,
                "confidence_reduction": conflict.confidence_reduction,
            },
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_prompt(new_body: str, existing_body: str) -> str:
    """Build the contradiction check prompt without str.format on untrusted input."""
    return (
        "Are these two statements contradictory?\n"
        "Statement A: " + new_body + "\n"
        "Statement B: " + existing_body + "\n"
        'Respond with JSON only: {"contradicts": true/false, "explanation": "..."}'
    )


async def _current_confidence(conn: Any, node_id: int) -> float:
    """Fetch the current confidence for a node within a transaction."""
    row = await conn.fetchval("SELECT confidence FROM node WHERE id = $1", node_id)
    return float(row) if row is not None else 0.5


async def _ask_llm_contradiction(new_body: str, candidate: NodeResult) -> tuple[bool, str]:
    """Ask the LLM whether two statements contradict each other."""
    prompt = _build_prompt(new_body, candidate.body)
    messages: list[MessageParam] = [{"role": "user", "content": prompt}]

    chunks = [
        event.text
        async for event in stream_response(
            messages,
            speed="reactive",
            max_tokens=_CONTRADICTION_MAX_TOKENS,
        )
        if isinstance(event, TextDelta)
    ]

    raw = "".join(chunks)
    return _parse_contradiction_response(raw)


def _parse_contradiction_response(raw: str) -> tuple[bool, str]:
    """Parse the LLM JSON response for contradiction detection."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("failed to parse contradiction LLM response", extra={"raw": raw[:200]})
        return False, ""

    if not isinstance(parsed, dict):
        log.warning("unexpected contradiction LLM response type", extra={"raw": raw[:200]})
        return False, ""

    return bool(parsed.get("contradicts", False)), str(parsed.get("explanation", ""))
