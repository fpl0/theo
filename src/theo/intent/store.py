"""Intent persistence: create, fetch, transition, and budget queries."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from theo.db import db
from theo.intent._types import IntentResult

if TYPE_CHECKING:
    from datetime import datetime

    from theo.intent._types import IntentState

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_INSERT_INTENT = """
INSERT INTO intent
    (type, state, base_priority, source_module, payload,
     deadline, budget_tokens, max_attempts, expires_at)
VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
RETURNING id
"""

# Dynamic priority: base + recency boost + deadline urgency.
# Recency boost: intents created within the last hour get up to +10.
# Deadline urgency: intents approaching deadline get up to +30.
_FETCH_NEXT = """
SELECT
    id, type, state, base_priority, source_module, payload,
    deadline, budget_tokens, attempts, max_attempts,
    result, error, created_at, updated_at, started_at,
    completed_at, expires_at,
    (
        base_priority
        + LEAST(10, GREATEST(0,
            10 - EXTRACT(EPOCH FROM (now() - created_at)) / 360))
        + CASE
            WHEN deadline IS NOT NULL AND deadline > now()
            THEN LEAST(30, GREATEST(0,
                30 * (1 - EXTRACT(EPOCH FROM (deadline - now()))
                    / EXTRACT(EPOCH FROM (deadline - created_at)))))
            ELSE 0
          END
    ) AS effective_priority
FROM intent
WHERE state IN ('proposed', 'approved')
  AND (expires_at IS NULL OR expires_at > now())
  AND attempts < max_attempts
ORDER BY effective_priority DESC, created_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED
"""

_UPDATE_STATE = """
UPDATE intent
SET state = $2
WHERE id = $1
"""

_START_INTENT = """
UPDATE intent
SET state = 'executing', started_at = now(), attempts = attempts + 1
WHERE id = $1
"""

_COMPLETE_INTENT = """
UPDATE intent
SET state = $2, completed_at = now(), result = $3::jsonb, error = $4
WHERE id = $1
"""

_EXPIRE_OVERDUE = """
UPDATE intent
SET state = 'expired'
WHERE state IN ('proposed', 'approved')
  AND expires_at IS NOT NULL
  AND expires_at <= now()
RETURNING id
"""

_DAILY_BUDGET_USAGE = """
SELECT COALESCE(SUM((result->>'tokens_used')::int), 0) AS total
FROM intent
WHERE state = 'completed'
  AND completed_at >= date_trunc('day', now())
"""

_QUEUE_DEPTH = """
SELECT count(*) AS depth
FROM intent
WHERE state IN ('proposed', 'approved')
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_intent(  # noqa: PLR0913
    *,
    intent_type: str,
    source_module: str,
    base_priority: int = 50,
    payload: dict[str, Any] | None = None,
    deadline: datetime | None = None,
    budget_tokens: int | None = None,
    max_attempts: int = 3,
    expires_at: datetime | None = None,
    state: IntentState = "proposed",
) -> int:
    """Insert a new intent into the queue. Returns the intent id."""
    with tracer.start_as_current_span(
        "intent.create",
        attributes={
            "intent.type": intent_type,
            "intent.source": source_module,
            "intent.priority": base_priority,
        },
    ):
        import json  # noqa: PLC0415

        row_id: int = await db.pool.fetchval(
            _INSERT_INTENT,
            intent_type,
            state,
            base_priority,
            source_module,
            json.dumps(payload or {}),
            deadline,
            budget_tokens,
            max_attempts,
            expires_at,
        )
        log.info(
            "created intent",
            extra={
                "intent_id": row_id,
                "intent_type": intent_type,
                "source_module": source_module,
                "base_priority": base_priority,
            },
        )
        return row_id


async def fetch_next() -> IntentResult | None:
    """Fetch the highest-priority actionable intent, locking the row.

    Must be called within a transaction for the ``FOR UPDATE SKIP LOCKED``
    to take effect. Returns ``None`` when the queue is empty.
    """
    with tracer.start_as_current_span("intent.fetch_next"):
        row = await db.pool.fetchrow(_FETCH_NEXT)
        if row is None:
            return None
        return _row_to_result(row, with_priority=True)


async def start_intent(intent_id: int) -> None:
    """Transition an intent to ``executing``."""
    with tracer.start_as_current_span(
        "intent.start",
        attributes={"intent.id": intent_id},
    ):
        await db.pool.execute(_START_INTENT, intent_id)
        log.info("started intent", extra={"intent_id": intent_id})


async def complete_intent(
    intent_id: int,
    *,
    state: IntentState = "completed",
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Mark an intent as completed or failed."""
    import json  # noqa: PLC0415

    with tracer.start_as_current_span(
        "intent.complete",
        attributes={"intent.id": intent_id, "intent.state": state},
    ):
        await db.pool.execute(
            _COMPLETE_INTENT,
            intent_id,
            state,
            json.dumps(result) if result else None,
            error,
        )
        log.info(
            "completed intent",
            extra={"intent_id": intent_id, "state": state},
        )


async def expire_overdue() -> list[int]:
    """Expire all intents past their ``expires_at``. Returns expired ids."""
    with tracer.start_as_current_span("intent.expire_overdue"):
        rows = await db.pool.fetch(_EXPIRE_OVERDUE)
        expired_ids = [row["id"] for row in rows]
        if expired_ids:
            log.info("expired intents", extra={"count": len(expired_ids), "ids": expired_ids})
        return expired_ids


async def get_daily_token_usage() -> int:
    """Sum of ``result.tokens_used`` for intents completed today."""
    with tracer.start_as_current_span("intent.daily_token_usage"):
        total: int = await db.pool.fetchval(_DAILY_BUDGET_USAGE)
        return total


async def get_queue_depth() -> int:
    """Count of actionable intents in the queue."""
    depth: int = await db.pool.fetchval(_QUEUE_DEPTH)
    return depth


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_result(
    row: Any,
    *,
    with_priority: bool = False,
) -> IntentResult:
    return IntentResult(
        id=row["id"],
        type=row["type"],
        state=row["state"],
        base_priority=row["base_priority"],
        source_module=row["source_module"],
        payload=row["payload"],
        deadline=row["deadline"],
        budget_tokens=row["budget_tokens"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        result=row["result"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        expires_at=row["expires_at"],
        effective_priority=row["effective_priority"] if with_priority else None,
    )
