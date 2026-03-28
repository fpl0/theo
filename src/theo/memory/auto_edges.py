"""Automatic edge creation from entity co-occurrence in episodes.

When Theo stores memories about related concepts in the same conversation,
this module links them via ``co_occurs`` edges. Weight increases with the
number of co-occurrences (more mentions together = stronger link).

Two entry points:

- ``record_mention`` — records that a node was mentioned in an episode.
- ``extract_and_link`` — scans a session for co-occurring nodes and creates edges.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import metrics, trace

from theo.db import db
from theo.memory.edges import store_edge

if TYPE_CHECKING:
    from uuid import UUID

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

_edges_counter = _meter.create_counter(
    "theo.memory.auto_edges.created",
    description="Total co-occurrence edges created automatically",
)

_CO_OCCURRENCE_WEIGHT_STEP = 0.2

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_INSERT_MENTION = """
INSERT INTO episode_node (episode_id, node_id, role)
VALUES ($1, $2, $3)
ON CONFLICT DO NOTHING
"""

# NOTE: This scans the entire session history, not just the current turn.
# For long sessions this is O(N²) in edge upserts per turn. Acceptable at
# current scale; revisit if sessions routinely exceed hundreds of episodes.
_CO_OCCURRENCES = """
SELECT
    en1.node_id AS node_a,
    en2.node_id AS node_b,
    count(*) AS co_count
FROM episode_node en1
INNER JOIN episode_node en2
    ON en1.episode_id = en2.episode_id
    AND en1.node_id < en2.node_id
INNER JOIN episode e
    ON e.id = en1.episode_id
WHERE e.session_id = $1
GROUP BY en1.node_id, en2.node_id
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def record_mention(
    episode_id: int,
    node_id: int,
    role: str = "mention",
) -> None:
    """Record that *node_id* was mentioned in *episode_id*.

    Inserts into ``episode_node``. No-op on duplicate (``ON CONFLICT DO NOTHING``).
    """
    with tracer.start_as_current_span(
        "auto_edge.record_mention",
        attributes={
            "episode.id": episode_id,
            "node.id": node_id,
            "mention.role": role,
        },
    ):
        await db.pool.execute(_INSERT_MENTION, episode_id, node_id, role)
        log.debug(
            "recorded mention",
            extra={"episode_id": episode_id, "node_id": node_id, "role": role},
        )


async def extract_and_link(session_id: UUID) -> int:
    """Create ``co_occurs`` edges for nodes that appear together in a session.

    Weight formula: ``min(1.0, co_count * _CO_OCCURRENCE_WEIGHT_STEP)`` — more
    co-occurrences produce a stronger link, capped at 1.0.

    Returns the number of edges created.
    """
    with tracer.start_as_current_span(
        "auto_edge.extract_and_link",
        attributes={"session.id": str(session_id)},
    ) as span:
        rows = await db.pool.fetch(_CO_OCCURRENCES, session_id)

        edges_created = 0
        for row in rows:
            node_a: int = row["node_a"]
            node_b: int = row["node_b"]
            co_count: int = row["co_count"]
            weight = min(1.0, co_count * _CO_OCCURRENCE_WEIGHT_STEP)

            await store_edge(
                source_id=node_a,
                target_id=node_b,
                label="co_occurs",
                weight=weight,
                meta={"co_count": co_count, "source": "auto_edge"},
            )
            edges_created += 1

        span.set_attribute("edges.created", edges_created)
        _edges_counter.add(edges_created)
        log.info(
            "extracted co-occurrence edges",
            extra={
                "session_id": str(session_id),
                "edges_created": edges_created,
            },
        )
        return edges_created
