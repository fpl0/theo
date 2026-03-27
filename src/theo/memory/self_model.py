"""Self-model operations: domain accuracy tracking."""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry import trace

from theo.db import db
from theo.memory._types import DomainResult

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_SELECT_ALL = """
SELECT id, domain, accuracy, total_predictions, correct_predictions,
       last_evaluated_at, created_at
FROM self_model_domain
ORDER BY domain
"""

_RECORD_OUTCOME = """
UPDATE self_model_domain
SET total_predictions = total_predictions + 1,
    correct_predictions = correct_predictions + CASE WHEN $2 THEN 1 ELSE 0 END,
    accuracy = (correct_predictions + CASE WHEN $2 THEN 1 ELSE 0 END)::real
             / (total_predictions + 1)::real,
    last_evaluated_at = now()
WHERE domain = $1
RETURNING id, domain, accuracy, total_predictions, correct_predictions,
          last_evaluated_at, created_at
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def read_domains() -> list[DomainResult]:
    """Return all self-model domains."""
    with tracer.start_as_current_span(
        "read_self_model_domains",
        attributes={"self_model.operation": "read_all"},
    ):
        rows = await db.pool.fetch(_SELECT_ALL)
        results = [_row_to_result(r) for r in rows]
        log.debug("read %d self-model domain(s)", len(results))
        return results


async def record_outcome(domain: str, *, correct: bool) -> DomainResult:
    """Record a prediction outcome for *domain*.

    Increments ``total_predictions``, conditionally increments
    ``correct_predictions``, recomputes ``accuracy``, and sets
    ``last_evaluated_at`` to now.

    Raises ``ValueError`` if *domain* does not exist.
    """
    with tracer.start_as_current_span(
        "record_self_model_outcome",
        attributes={"self_model.domain": domain, "self_model.correct": correct},
    ):
        row = await db.pool.fetchrow(_RECORD_OUTCOME, domain, correct)
        if row is None:
            msg = f"unknown self-model domain {domain!r}"
            raise ValueError(msg)
        result = _row_to_result(row)
        log.info(
            "recorded outcome",
            extra={"domain": domain, "correct": correct, "accuracy": result.accuracy},
        )
        return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_result(row: Any) -> DomainResult:
    return DomainResult(
        id=row["id"],
        domain=row["domain"],
        accuracy=row["accuracy"],
        total_predictions=row["total_predictions"],
        correct_predictions=row["correct_predictions"],
        last_evaluated_at=row["last_evaluated_at"],
        created_at=row["created_at"],
    )
