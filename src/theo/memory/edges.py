"""Knowledge graph edge operations: store, retrieve, traverse, expire."""

from __future__ import annotations

import logging
from typing import Any, Literal

from opentelemetry import trace

from theo.db import db
from theo.memory._types import EdgeResult, TraversalResult

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

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

_SELECT_EDGES_OUTGOING = """
SELECT id, source_id, target_id, label, weight, meta, valid_from, valid_to, created_at
FROM edge
WHERE source_id = $1 AND valid_to IS NULL
ORDER BY created_at
"""

_SELECT_EDGES_OUTGOING_BY_LABEL = """
SELECT id, source_id, target_id, label, weight, meta, valid_from, valid_to, created_at
FROM edge
WHERE source_id = $1 AND label = $2 AND valid_to IS NULL
ORDER BY created_at
"""

_SELECT_EDGES_INCOMING = """
SELECT id, source_id, target_id, label, weight, meta, valid_from, valid_to, created_at
FROM edge
WHERE target_id = $1 AND valid_to IS NULL
ORDER BY created_at
"""

_SELECT_EDGES_INCOMING_BY_LABEL = """
SELECT id, source_id, target_id, label, weight, meta, valid_from, valid_to, created_at
FROM edge
WHERE target_id = $1 AND label = $2 AND valid_to IS NULL
ORDER BY created_at
"""

_TRAVERSE = """
WITH RECURSIVE graph AS (
    SELECT
        target_id AS node_id,
        1 AS depth,
        ARRAY[source_id, target_id] AS path,
        weight AS cumulative_weight
    FROM edge
    WHERE source_id = $1
        AND valid_to IS NULL

    UNION ALL

    SELECT
        e.target_id,
        g.depth + 1,
        g.path || e.target_id,
        g.cumulative_weight * e.weight
    FROM graph g
    INNER JOIN edge e ON e.source_id = g.node_id
    WHERE e.valid_to IS NULL
        AND g.depth < $2
        AND e.target_id <> ALL(g.path)
)
SELECT DISTINCT ON (node_id)
    node_id, depth, path, cumulative_weight
FROM graph
ORDER BY node_id, cumulative_weight DESC
"""

_TRAVERSE_BY_LABEL = """
WITH RECURSIVE graph AS (
    SELECT
        target_id AS node_id,
        1 AS depth,
        ARRAY[source_id, target_id] AS path,
        weight AS cumulative_weight
    FROM edge
    WHERE source_id = $1
        AND valid_to IS NULL
        AND label = $3

    UNION ALL

    SELECT
        e.target_id,
        g.depth + 1,
        g.path || e.target_id,
        g.cumulative_weight * e.weight
    FROM graph g
    INNER JOIN edge e ON e.source_id = g.node_id
    WHERE e.valid_to IS NULL
        AND e.label = $3
        AND g.depth < $2
        AND e.target_id <> ALL(g.path)
)
SELECT DISTINCT ON (node_id)
    node_id, depth, path, cumulative_weight
FROM graph
ORDER BY node_id, cumulative_weight DESC
"""

_EXPIRE_EDGE_BY_ID = """
UPDATE edge
SET valid_to = now()
WHERE id = $1 AND valid_to IS NULL
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

type EdgeDirection = Literal["outgoing", "incoming", "both"]


async def store_edge(
    *,
    source_id: int,
    target_id: int,
    label: str,
    weight: float = 1.0,
    meta: dict[str, Any] | None = None,
) -> int:
    """Create an edge, expiring any existing active edge with the same key.

    Returns the new edge's id.
    """
    with tracer.start_as_current_span(
        "store_edge",
        attributes={
            "edge.label": label,
            "edge.source_id": source_id,
            "edge.target_id": target_id,
        },
    ):
        async with db.pool.acquire() as conn, conn.transaction():
            await conn.execute(_EXPIRE_ACTIVE_EDGE, source_id, target_id, label)
            edge_id: int = await conn.fetchval(
                _INSERT_EDGE,
                source_id,
                target_id,
                label,
                weight,
                meta if meta is not None else {},
            )

        log.info(
            "stored edge",
            extra={
                "edge_id": edge_id,
                "label": label,
                "source_id": source_id,
                "target_id": target_id,
            },
        )
        return edge_id


async def get_edges(
    node_id: int,
    *,
    direction: EdgeDirection = "outgoing",
    label: str | None = None,
) -> list[EdgeResult]:
    """Retrieve active edges for a node.

    *direction* controls which edges to return: ``outgoing`` (default),
    ``incoming``, or ``both``.  Optionally filter by *label*.
    """
    with tracer.start_as_current_span(
        "get_edges",
        attributes={"node.id": node_id, "edge.direction": direction},
    ):
        if direction == "both":
            outgoing = await _fetch_edges(node_id, direction="outgoing", label=label)
            incoming = await _fetch_edges(node_id, direction="incoming", label=label)
            results = outgoing + incoming
        else:
            results = await _fetch_edges(node_id, direction=direction, label=label)

        log.debug("get_edges returned %d edge(s)", len(results))
        return results


async def traverse(
    start_id: int,
    *,
    max_depth: int = 2,
    label: str | None = None,
) -> list[TraversalResult]:
    """Traverse outgoing edges from *start_id* up to *max_depth* hops.

    Returns unique reachable nodes ordered by cumulative weight descending.
    """
    with tracer.start_as_current_span(
        "traverse_graph",
        attributes={"start.id": start_id, "traverse.max_depth": max_depth},
    ):
        if label is not None:
            rows = await db.pool.fetch(_TRAVERSE_BY_LABEL, start_id, max_depth, label)
        else:
            rows = await db.pool.fetch(_TRAVERSE, start_id, max_depth)

        results = [_row_to_traversal(r) for r in rows]
        results.sort(key=lambda r: r.cumulative_weight, reverse=True)
        log.debug("traverse from %d returned %d node(s)", start_id, len(results))
        return results


async def expire_edge(edge_id: int) -> bool:
    """Expire an edge by setting ``valid_to`` to now.

    Returns ``True`` if the edge was active and is now expired,
    ``False`` if already expired or not found.
    """
    with tracer.start_as_current_span(
        "expire_edge",
        attributes={"edge.id": edge_id},
    ):
        result = await db.pool.execute(_EXPIRE_EDGE_BY_ID, edge_id)
        updated = result == "UPDATE 1"
        log.info("expire_edge", extra={"edge_id": edge_id, "updated": updated})
        return updated


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _fetch_edges(
    node_id: int,
    *,
    direction: Literal["outgoing", "incoming"],
    label: str | None,
) -> list[EdgeResult]:
    if direction == "outgoing":
        if label is not None:
            rows = await db.pool.fetch(_SELECT_EDGES_OUTGOING_BY_LABEL, node_id, label)
        else:
            rows = await db.pool.fetch(_SELECT_EDGES_OUTGOING, node_id)
    elif label is not None:
        rows = await db.pool.fetch(_SELECT_EDGES_INCOMING_BY_LABEL, node_id, label)
    else:
        rows = await db.pool.fetch(_SELECT_EDGES_INCOMING, node_id)

    return [_row_to_edge(r) for r in rows]


def _row_to_edge(row: Any) -> EdgeResult:
    return EdgeResult(
        id=row["id"],
        source_id=row["source_id"],
        target_id=row["target_id"],
        label=row["label"],
        weight=row["weight"],
        meta=row["meta"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        created_at=row["created_at"],
    )


def _row_to_traversal(row: Any) -> TraversalResult:
    return TraversalResult(
        node_id=row["node_id"],
        depth=row["depth"],
        path=list(row["path"]),
        cumulative_weight=row["cumulative_weight"],
    )
