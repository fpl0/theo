"""Knowledge graph node operations: store, search, retrieve."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from theo.db import db
from theo.embeddings import embedder
from theo.errors import PrivacyViolationError
from theo.memory._types import NodeResult
from theo.memory.privacy import evaluate

if TYPE_CHECKING:
    from theo.memory._types import SensitivityLevel, TrustTier

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_INSERT_NODE = """
INSERT INTO node (kind, body, embedding, trust, confidence, importance, sensitivity, meta)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING id
"""

_SELECT_NODE = """
SELECT
    id, kind, body, trust, confidence, importance, sensitivity, meta, created_at
FROM node
WHERE id = $1
"""

_SEARCH_NODES = """
SELECT
    id, kind, body, trust, confidence, importance, sensitivity, meta, created_at,
    1 - (embedding <=> $1) AS similarity
FROM node
WHERE embedding IS NOT NULL
ORDER BY embedding <=> $1
LIMIT $2
"""

_SEARCH_NODES_BY_KIND = """
SELECT
    id, kind, body, trust, confidence, importance, sensitivity, meta, created_at,
    1 - (embedding <=> $1) AS similarity
FROM node
WHERE embedding IS NOT NULL AND kind = $2
ORDER BY embedding <=> $1
LIMIT $3
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def store_node(  # noqa: PLR0913
    *,
    kind: str,
    body: str,
    trust: TrustTier = "inferred",
    confidence: float = 0.5,
    importance: float = 0.5,
    sensitivity: SensitivityLevel = "normal",
    meta: dict[str, Any] | None = None,
) -> int:
    """Insert a node with auto-generated embedding and tsvector.

    Returns the new node's id.
    """
    with tracer.start_as_current_span(
        "store_node",
        attributes={"node.kind": kind, "memory.operation": "store"},
    ):
        decision = evaluate(body, trust=trust, sensitivity=sensitivity)
        if not decision.allowed:
            raise PrivacyViolationError(decision.reason)
        sensitivity = decision.sensitivity

        vec = await embedder.embed_one(body)
        row_id: int = await db.pool.fetchval(
            _INSERT_NODE,
            kind,
            body,
            vec,
            trust,
            confidence,
            importance,
            sensitivity,
            meta if meta is not None else {},
        )
        log.info("stored node", extra={"node_id": row_id, "kind": kind})
        return row_id


async def search_nodes(
    query: str,
    *,
    limit: int = 10,
    kind: str | None = None,
) -> list[NodeResult]:
    """Search nodes by cosine similarity to *query*.

    Returns results ordered closest-first. Optionally filter by *kind*.
    """
    with tracer.start_as_current_span(
        "search_nodes",
        attributes={"memory.operation": "search", "search.limit": limit},
    ):
        vec = await embedder.embed_one(query)

        if kind is not None:
            rows = await db.pool.fetch(_SEARCH_NODES_BY_KIND, vec, kind, limit)
        else:
            rows = await db.pool.fetch(_SEARCH_NODES, vec, limit)

        results = [_row_to_result(r, with_similarity=True) for r in rows]
        log.debug("search returned %d node(s)", len(results))
        return results


async def get_node(node_id: int) -> NodeResult | None:
    """Retrieve a single node by id, or ``None`` if it doesn't exist."""
    with tracer.start_as_current_span(
        "get_node",
        attributes={"node.id": node_id, "memory.operation": "get"},
    ):
        row = await db.pool.fetchrow(_SELECT_NODE, node_id)
        if row is None:
            return None
        return _row_to_result(row)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_result(
    row: Any,
    *,
    with_similarity: bool = False,
) -> NodeResult:
    return NodeResult(
        id=row["id"],
        kind=row["kind"],
        body=row["body"],
        trust=row["trust"],
        confidence=row["confidence"],
        importance=row["importance"],
        sensitivity=row["sensitivity"],
        meta=row["meta"],
        created_at=row["created_at"],
        similarity=row["similarity"] if with_similarity else None,
    )
