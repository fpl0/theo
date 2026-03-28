"""Knowledge graph edge operations: store, retrieve, traverse, expire."""

from __future__ import annotations

import logging
from typing import Any, Literal

from opentelemetry import metrics, trace

from theo.db import db
from theo.memory._edge_sql import (
    EXPIRE_ACTIVE_EDGE,
    EXPIRE_EDGE_BY_ID,
    INSERT_EDGE,
    SELECT_EDGES_INCOMING,
    SELECT_EDGES_INCOMING_BY_LABEL,
    SELECT_EDGES_OUTGOING,
    SELECT_EDGES_OUTGOING_BY_LABEL,
    TRAVERSE,
    TRAVERSE_BY_LABEL,
)
from theo.memory._types import EdgeResult, TraversalResult

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)
_edges_stored = _meter.create_counter(
    "theo.memory.edges.stored",
    description="Total edges stored in the knowledge graph",
)

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
            "edge.weight": weight,
        },
    ):
        async with db.pool.acquire() as conn, conn.transaction():
            await conn.execute(EXPIRE_ACTIVE_EDGE, source_id, target_id, label)
            edge_id: int = await conn.fetchval(
                INSERT_EDGE,
                source_id,
                target_id,
                label,
                weight,
                meta if meta is not None else {},
            )

        _edges_stored.add(1, {"edge.label": label})
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
        attributes={
            "node.id": node_id,
            "edge.direction": direction,
            **({"edge.label": label} if label is not None else {}),
        },
    ):
        if direction == "both":
            outgoing = await _fetch_edges(node_id, direction="outgoing", label=label)
            incoming = await _fetch_edges(node_id, direction="incoming", label=label)
            results = outgoing + incoming
        else:
            results = await _fetch_edges(node_id, direction=direction, label=label)

        log.debug(
            "fetched edges",
            extra={"node_id": node_id, "count": len(results), "direction": direction},
        )
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
        attributes={
            "start.id": start_id,
            "traverse.max_depth": max_depth,
            **({"traverse.label": label} if label is not None else {}),
        },
    ):
        if label is not None:
            rows = await db.pool.fetch(TRAVERSE_BY_LABEL, start_id, max_depth, label)
        else:
            rows = await db.pool.fetch(TRAVERSE, start_id, max_depth)

        results = [_row_to_traversal(r) for r in rows]
        results.sort(key=lambda r: r.cumulative_weight, reverse=True)
        log.debug(
            "traversed graph",
            extra={"start_id": start_id, "count": len(results), "max_depth": max_depth},
        )
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
        result = await db.pool.execute(EXPIRE_EDGE_BY_ID, edge_id)
        updated = result == "UPDATE 1"
        log.info("expired edge", extra={"edge_id": edge_id, "updated": updated})
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
            rows = await db.pool.fetch(SELECT_EDGES_OUTGOING_BY_LABEL, node_id, label)
        else:
            rows = await db.pool.fetch(SELECT_EDGES_OUTGOING, node_id)
    elif label is not None:
        rows = await db.pool.fetch(SELECT_EDGES_INCOMING_BY_LABEL, node_id, label)
    else:
        rows = await db.pool.fetch(SELECT_EDGES_INCOMING, node_id)

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
        path=tuple(row["path"]),
        cumulative_weight=row["cumulative_weight"],
    )
