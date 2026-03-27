"""Hybrid retrieval with Reciprocal Rank Fusion (RRF).

Fuses three signals — vector similarity, full-text search, and graph
traversal — into a single ranked list using RRF scoring.  Each signal
contributes ``1/(k + rank)`` for nodes it contains; nodes appearing in
multiple signals accumulate higher scores.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from opentelemetry import metrics, trace

from theo.config import get_settings
from theo.db import db
from theo.embeddings import embedder
from theo.memory._types import NodeResult

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

_duration_histogram = meter.create_histogram(
    "theo.retrieval.duration",
    unit="ms",
    description="Hybrid search latency",
)

# ---------------------------------------------------------------------------
# SQL — single RRF query with seven CTEs
# ---------------------------------------------------------------------------
#
# Parameters:
#   $1  embedding vector (for vector search)
#   $2  candidate limit per signal
#   $3  query text (for FTS via plainto_tsquery)
#   $4  RRF constant k
#   $5  graph traversal max depth
#   $6  graph seed count (top-N vector hits for traversal seeds)
#   $7  final result limit
#
# The query returns per-signal boolean flags (in_vector, in_fts, in_graph)
# so the caller can count hits per signal without extra queries.

_HYBRID_SEARCH = """
WITH vector_ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (ORDER BY embedding <=> $1) AS rank
    FROM node
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> $1
    LIMIT $2
),
fts_ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', $3)) DESC
        ) AS rank
    FROM node
    WHERE tsv @@ plainto_tsquery('english', $3)
    ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', $3)) DESC
    LIMIT $2
),
graph_seeds AS (
    SELECT id FROM vector_ranked WHERE rank <= $6
),
graph_traversal AS (
    SELECT
        e.target_id AS node_id,
        1 AS depth,
        ARRAY[e.source_id, e.target_id] AS path,
        e.weight AS cumulative_weight
    FROM edge e
    INNER JOIN graph_seeds s ON e.source_id = s.id
    WHERE e.valid_to IS NULL

    UNION ALL

    SELECT
        e.target_id,
        g.depth + 1,
        g.path || e.target_id,
        g.cumulative_weight * e.weight
    FROM graph_traversal g
    INNER JOIN edge e ON e.source_id = g.node_id
    WHERE e.valid_to IS NULL
        AND g.depth < $5
        AND e.target_id <> ALL(g.path)
),
graph_deduped AS (
    SELECT DISTINCT ON (node_id)
        node_id, cumulative_weight
    FROM graph_traversal
    ORDER BY node_id, cumulative_weight DESC
),
graph_ranked AS (
    SELECT
        node_id AS id,
        ROW_NUMBER() OVER (ORDER BY cumulative_weight DESC) AS rank
    FROM graph_deduped
),
rrf_fused AS (
    SELECT
        COALESCE(v.id, f.id, g.id) AS id,
        COALESCE(1.0 / ($4 + v.rank), 0)
            + COALESCE(1.0 / ($4 + f.rank), 0)
            + COALESCE(1.0 / ($4 + g.rank), 0) AS score,
        v.id IS NOT NULL AS in_vector,
        f.id IS NOT NULL AS in_fts,
        g.id IS NOT NULL AS in_graph
    FROM vector_ranked v
    FULL OUTER JOIN fts_ranked f ON v.id = f.id
    FULL OUTER JOIN graph_ranked g ON COALESCE(v.id, f.id) = g.id
)
SELECT
    n.id,
    n.kind,
    n.body,
    n.trust,
    n.confidence,
    n.importance,
    n.sensitivity,
    n.meta,
    n.created_at,
    r.score AS similarity,
    r.in_vector,
    r.in_fts,
    r.in_graph
FROM rrf_fused r
INNER JOIN node n ON n.id = r.id
ORDER BY r.score DESC
LIMIT $7
"""

_HYBRID_SEARCH_BY_KIND = """
WITH vector_ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (ORDER BY embedding <=> $1) AS rank
    FROM node
    WHERE embedding IS NOT NULL AND kind = $8
    ORDER BY embedding <=> $1
    LIMIT $2
),
fts_ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', $3)) DESC
        ) AS rank
    FROM node
    WHERE tsv @@ plainto_tsquery('english', $3) AND kind = $8
    ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', $3)) DESC
    LIMIT $2
),
graph_seeds AS (
    SELECT id FROM vector_ranked WHERE rank <= $6
),
graph_traversal AS (
    SELECT
        e.target_id AS node_id,
        1 AS depth,
        ARRAY[e.source_id, e.target_id] AS path,
        e.weight AS cumulative_weight
    FROM edge e
    INNER JOIN graph_seeds s ON e.source_id = s.id
    WHERE e.valid_to IS NULL

    UNION ALL

    SELECT
        e.target_id,
        g.depth + 1,
        g.path || e.target_id,
        g.cumulative_weight * e.weight
    FROM graph_traversal g
    INNER JOIN edge e ON e.source_id = g.node_id
    WHERE e.valid_to IS NULL
        AND g.depth < $5
        AND e.target_id <> ALL(g.path)
),
graph_deduped AS (
    SELECT DISTINCT ON (node_id)
        node_id, cumulative_weight
    FROM graph_traversal
    ORDER BY node_id, cumulative_weight DESC
),
graph_ranked AS (
    SELECT
        node_id AS id,
        ROW_NUMBER() OVER (ORDER BY cumulative_weight DESC) AS rank
    FROM graph_deduped
),
rrf_fused AS (
    SELECT
        COALESCE(v.id, f.id, g.id) AS id,
        COALESCE(1.0 / ($4 + v.rank), 0)
            + COALESCE(1.0 / ($4 + f.rank), 0)
            + COALESCE(1.0 / ($4 + g.rank), 0) AS score,
        v.id IS NOT NULL AS in_vector,
        f.id IS NOT NULL AS in_fts,
        g.id IS NOT NULL AS in_graph
    FROM vector_ranked v
    FULL OUTER JOIN fts_ranked f ON v.id = f.id
    FULL OUTER JOIN graph_ranked g ON COALESCE(v.id, f.id) = g.id
)
SELECT
    n.id,
    n.kind,
    n.body,
    n.trust,
    n.confidence,
    n.importance,
    n.sensitivity,
    n.meta,
    n.created_at,
    r.score AS similarity,
    r.in_vector,
    r.in_fts,
    r.in_graph
FROM rrf_fused r
INNER JOIN node n ON n.id = r.id
ORDER BY r.score DESC
LIMIT $7
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def hybrid_search(
    query: str,
    *,
    limit: int = 10,
    kind: str | None = None,
) -> list[NodeResult]:
    """Search nodes using RRF fusion of vector, FTS, and graph signals.

    The ``similarity`` field on each result carries the fused RRF score
    (higher is better, but not bounded to [0, 1]).

    Degrades gracefully when signals are absent: empty graph → vector + FTS,
    no FTS matches → vector + graph, missing embeddings → excluded naturally.
    """
    cfg = get_settings()
    start = time.monotonic()

    try:
        with tracer.start_as_current_span(
            "hybrid_search",
            attributes={
                "search.limit": limit,
                **({"search.kind": kind} if kind is not None else {}),
            },
        ) as span:
            vec = await embedder.embed_one(query)

            if kind is not None:
                rows = await db.pool.fetch(
                    _HYBRID_SEARCH_BY_KIND,
                    vec,
                    cfg.retrieval_candidate_limit,
                    query,
                    cfg.retrieval_rrf_k,
                    cfg.retrieval_graph_max_depth,
                    cfg.retrieval_graph_seed_count,
                    limit,
                    kind,
                )
            else:
                rows = await db.pool.fetch(
                    _HYBRID_SEARCH,
                    vec,
                    cfg.retrieval_candidate_limit,
                    query,
                    cfg.retrieval_rrf_k,
                    cfg.retrieval_graph_max_depth,
                    cfg.retrieval_graph_seed_count,
                    limit,
                )

            results = [_row_to_result(r) for r in rows]

            vector_hits = sum(1 for r in rows if r["in_vector"])
            fts_hits = sum(1 for r in rows if r["in_fts"])
            graph_hits = sum(1 for r in rows if r["in_graph"])

            span.set_attribute("search.vector_hits", vector_hits)
            span.set_attribute("search.fts_hits", fts_hits)
            span.set_attribute("search.graph_hits", graph_hits)
            span.set_attribute("search.result_count", len(results))

            log.info(
                "hybrid search completed",
                extra={
                    "result_count": len(results),
                    "vector_hits": vector_hits,
                    "fts_hits": fts_hits,
                    "graph_hits": graph_hits,
                },
            )
            return results
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        _duration_histogram.record(elapsed_ms)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_result(row: Any) -> NodeResult:
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
        similarity=row["similarity"],
    )
