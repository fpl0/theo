"""Episodic memory operations: store, list, search."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from theo.db import db
from theo.embeddings import embedder
from theo.errors import PrivacyViolationError
from theo.memory._types import EpisodeResult
from theo.memory.privacy import escalate_sensitivity, evaluate

if TYPE_CHECKING:
    from uuid import UUID

    from theo.memory._types import (
        EpisodeChannel,
        EpisodeRole,
        SensitivityLevel,
        TrustTier,
    )

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_INSERT_EPISODE = """
INSERT INTO episode
    (session_id, channel, role, body, embedding, trust, importance, sensitivity, meta)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
RETURNING id
"""

_LIST_EPISODES = """
SELECT
    id, session_id, channel, role, body, trust, importance, sensitivity, meta, created_at
FROM episode
WHERE session_id = $1
ORDER BY created_at ASC
LIMIT $2
"""

_SEARCH_EPISODES = """
SELECT
    id, session_id, channel, role, body, trust, importance, sensitivity, meta, created_at,
    1 - (embedding <=> $1) AS similarity
FROM episode
WHERE embedding IS NOT NULL
ORDER BY embedding <=> $1
LIMIT $2
"""

_SEARCH_EPISODES_BY_SESSION = """
SELECT
    id, session_id, channel, role, body, trust, importance, sensitivity, meta, created_at,
    1 - (embedding <=> $1) AS similarity
FROM episode
WHERE embedding IS NOT NULL AND session_id = $2
ORDER BY embedding <=> $1
LIMIT $3
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def store_episode(  # noqa: PLR0913
    *,
    session_id: UUID,
    channel: EpisodeChannel = "internal",
    role: EpisodeRole,
    body: str,
    trust: TrustTier = "owner",
    importance: float = 0.5,
    sensitivity: SensitivityLevel = "normal",
    meta: dict[str, Any] | None = None,
) -> int:
    """Insert an episode with auto-generated embedding.

    Returns the new episode's id.
    """
    with tracer.start_as_current_span(
        "store_episode",
        attributes={"session.id": str(session_id), "memory.operation": "store"},
    ):
        decision = evaluate(
            body,
            trust=trust,
            sensitivity=sensitivity,
            channel=channel,
        )
        if not decision.allowed:
            raise PrivacyViolationError(decision.reason)
        sensitivity = escalate_sensitivity(sensitivity, decision.sensitivity)

        vec = await embedder.embed_one(body)
        row_id: int = await db.pool.fetchval(
            _INSERT_EPISODE,
            session_id,
            channel,
            role,
            body,
            vec,
            trust,
            importance,
            sensitivity,
            meta if meta is not None else {},
        )
        log.info(
            "stored episode",
            extra={"episode_id": row_id, "session_id": str(session_id), "role": role},
        )
        return row_id


async def list_episodes(
    session_id: UUID,
    *,
    limit: int = 50,
) -> list[EpisodeResult]:
    """List episodes for a session in chronological order (oldest first)."""
    with tracer.start_as_current_span(
        "list_episodes",
        attributes={"session.id": str(session_id), "memory.operation": "list"},
    ):
        rows = await db.pool.fetch(_LIST_EPISODES, session_id, limit)
        results = [_row_to_result(r) for r in rows]
        log.debug("listed %d episode(s) for session %s", len(results), session_id)
        return results


async def search_episodes(
    query: str,
    *,
    limit: int = 10,
    session_id: UUID | None = None,
) -> list[EpisodeResult]:
    """Search episodes by cosine similarity to *query*.

    Returns results ordered closest-first. Optionally filter by *session_id*.
    """
    with tracer.start_as_current_span(
        "search_episodes",
        attributes={"memory.operation": "search", "search.limit": limit},
    ):
        vec = await embedder.embed_one(query)

        if session_id is not None:
            rows = await db.pool.fetch(_SEARCH_EPISODES_BY_SESSION, vec, session_id, limit)
        else:
            rows = await db.pool.fetch(_SEARCH_EPISODES, vec, limit)

        results = [_row_to_result(r, with_similarity=True) for r in rows]
        log.debug("search returned %d episode(s)", len(results))
        return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_result(
    row: Any,
    *,
    with_similarity: bool = False,
) -> EpisodeResult:
    return EpisodeResult(
        id=row["id"],
        session_id=row["session_id"],
        channel=row["channel"],
        role=row["role"],
        body=row["body"],
        trust=row["trust"],
        importance=row["importance"],
        sensitivity=row["sensitivity"],
        meta=row["meta"],
        created_at=row["created_at"],
        similarity=row["similarity"] if with_similarity else None,
    )
