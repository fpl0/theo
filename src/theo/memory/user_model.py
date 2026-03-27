"""Structured user model: read, update, and query tracked dimensions."""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry import trace

from theo.db import db
from theo.memory._types import DimensionResult

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_SELECT_ALL = """
SELECT id, framework, dimension, value, confidence, evidence_count, updated_at
FROM user_model_dimension
ORDER BY framework, dimension
"""

_SELECT_BY_FRAMEWORK = """
SELECT id, framework, dimension, value, confidence, evidence_count, updated_at
FROM user_model_dimension
WHERE framework = $1
ORDER BY dimension
"""

_SELECT_ONE = """
SELECT id, framework, dimension, value, confidence, evidence_count, updated_at
FROM user_model_dimension
WHERE framework = $1 AND dimension = $2
"""

_UPDATE = """
UPDATE user_model_dimension
SET value = $3,
    evidence_count = evidence_count + 1,
    confidence = LEAST(1.0, (evidence_count + 1)::real / 10.0)
WHERE framework = $1 AND dimension = $2
RETURNING id, framework, dimension, value, confidence, evidence_count, updated_at
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def read_dimensions(*, framework: str | None = None) -> list[DimensionResult]:
    """Return all dimensions, optionally filtered by *framework*."""
    with tracer.start_as_current_span(
        "read_user_model_dimensions",
        attributes={"user_model.operation": "read_dimensions"},
    ):
        if framework is not None:
            rows = await db.pool.fetch(_SELECT_BY_FRAMEWORK, framework)
        else:
            rows = await db.pool.fetch(_SELECT_ALL)
        results = [_row_to_result(r) for r in rows]
        log.debug("read %d user model dimension(s)", len(results))
        return results


async def get_dimension(framework: str, dimension: str) -> DimensionResult | None:
    """Return a single dimension or ``None`` if not found."""
    with tracer.start_as_current_span(
        "get_user_model_dimension",
        attributes={
            "user_model.framework": framework,
            "user_model.dimension": dimension,
            "user_model.operation": "get_dimension",
        },
    ):
        row = await db.pool.fetchrow(_SELECT_ONE, framework, dimension)
        if row is None:
            return None
        return _row_to_result(row)


async def update_dimension(
    framework: str,
    dimension: str,
    *,
    value: dict[str, Any],
    reason: str | None = None,
) -> DimensionResult:
    """Update a dimension's value, increment evidence, and recompute confidence.

    Raises ``LookupError`` if the framework/dimension pair does not exist.
    """
    with tracer.start_as_current_span(
        "update_user_model_dimension",
        attributes={
            "user_model.framework": framework,
            "user_model.dimension": dimension,
            "user_model.operation": "update_dimension",
        },
    ):
        row = await db.pool.fetchrow(_UPDATE, framework, dimension, value)
        if row is None:
            msg = f"user model dimension {framework!r}/{dimension!r} not found"
            raise LookupError(msg)

        result = _row_to_result(row)
        log.info(
            "updated user model dimension",
            extra={
                "framework": framework,
                "dimension": dimension,
                "evidence_count": result.evidence_count,
                "confidence": result.confidence,
                "reason": reason,
            },
        )
        return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_result(row: Any) -> DimensionResult:
    return DimensionResult(
        id=row["id"],
        framework=row["framework"],
        dimension=row["dimension"],
        value=row["value"],
        confidence=row["confidence"],
        evidence_count=row["evidence_count"],
        updated_at=row["updated_at"],
    )
