"""Knowledge graph node operations: store, search, retrieve."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from theo.config import get_settings
from theo.db import db
from theo.embeddings import embedder
from theo.errors import PrivacyViolationError
from theo.memory._types import NodeResult
from theo.memory.privacy import evaluate

if TYPE_CHECKING:
    from theo.memory._types import SensitivityLevel, TrustTier

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

_background_tasks: set[asyncio.Task[None]] = set()

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

        if get_settings().contradiction_check_enabled:
            task = asyncio.create_task(
                _run_contradiction_check(row_id, body, kind),
                context=contextvars.copy_context(),
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

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


async def drain_background_tasks(*, drain_timeout: float = 5.0) -> None:
    """Wait for in-flight contradiction checks to finish.

    Called during shutdown before the database pool is closed.
    """
    if not _background_tasks:
        return
    log.info("draining %d background task(s)", len(_background_tasks))
    _done, pending = await asyncio.wait(_background_tasks, timeout=drain_timeout)
    if pending:
        log.warning("timed out draining %d background task(s)", len(pending))
        for task in pending:
            task.cancel()


async def _run_contradiction_check(node_id: int, body: str, kind: str) -> None:
    """Fire-and-forget contradiction check — errors are logged, never raised."""
    try:
        from theo.memory.contradictions import (  # noqa: PLC0415
            check_contradiction,
            resolve_contradiction,
        )

        conflict = await check_contradiction(body, kind, exclude_id=node_id)
        if conflict is not None:
            await resolve_contradiction(node_id, conflict)
    except Exception:
        log.exception("contradiction check failed", extra={"node_id": node_id})
