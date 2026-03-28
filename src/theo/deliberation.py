"""Deliberation store — persistent state for multi-step reasoning sessions.

Stores the full lifecycle of a deliberation from framing through synthesis.
Each row holds the complete state (flat JSONB for phase outputs) so recovery
and context assembly require only a single-row read.

Deliberation is a conversation concern, not a memory concern — it represents
*how* Theo thinks, not *what* Theo remembers.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any, Literal

from opentelemetry import metrics, trace

from theo.db import db

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

_deliberations_created = _meter.create_counter(
    "theo.deliberation.created",
    description="Total deliberation sessions created",
)
_deliberations_completed = _meter.create_counter(
    "theo.deliberation.completed",
    description="Total deliberation sessions completed",
)
_phase_transitions = _meter.create_counter(
    "theo.deliberation.phase_transitions",
    description="Total phase transitions across all deliberations",
)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

type DeliberationPhase = Literal[
    "frame", "gather", "generate", "evaluate", "synthesize", "complete"
]
type DeliberationStatus = Literal["running", "completed", "failed", "cancelled"]


@dataclasses.dataclass(frozen=True, slots=True)
class DeliberationState:
    """Immutable snapshot of a deliberation row."""

    id: int
    deliberation_id: UUID
    session_id: UUID
    question: str
    phase: DeliberationPhase
    phase_outputs: dict[str, Any]
    status: DeliberationStatus
    created_at: datetime
    completed_at: datetime | None
    updated_at: datetime
    delivered: bool


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_INSERT = """
INSERT INTO deliberation (session_id, question)
VALUES ($1, $2)
RETURNING
    id,
    deliberation_id,
    session_id,
    question,
    phase,
    phase_outputs,
    status,
    created_at,
    completed_at,
    updated_at,
    delivered
"""

_SELECT_BY_ID = """
SELECT
    id,
    deliberation_id,
    session_id,
    question,
    phase,
    phase_outputs,
    status,
    created_at,
    completed_at,
    updated_at,
    delivered
FROM deliberation
WHERE deliberation_id = $1
"""

_UPDATE_PHASE = """
UPDATE deliberation
SET
    phase = $2,
    phase_outputs = phase_outputs || jsonb_build_object($3, to_jsonb($4::text))
WHERE deliberation_id = $1
  AND status = 'running'
RETURNING id
"""

_COMPLETE = """
UPDATE deliberation
SET status = $2, completed_at = now()
WHERE deliberation_id = $1
  AND status = 'running'
RETURNING id
"""

_MARK_DELIVERED = """
UPDATE deliberation
SET delivered = TRUE
WHERE deliberation_id = $1
  AND status = 'completed'
  AND NOT delivered
RETURNING id
"""

_LIST_PENDING_DELIVERY = """
SELECT
    id,
    deliberation_id,
    session_id,
    question,
    phase,
    phase_outputs,
    status,
    created_at,
    completed_at,
    updated_at,
    delivered
FROM deliberation
WHERE status = 'completed' AND NOT delivered
ORDER BY created_at
LIMIT $1
"""

_LIST_ACTIVE = """
SELECT
    id,
    deliberation_id,
    session_id,
    question,
    phase,
    phase_outputs,
    status,
    created_at,
    completed_at,
    updated_at,
    delivered
FROM deliberation
WHERE session_id = $1 AND status = 'running'
ORDER BY created_at
LIMIT $2
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_deliberation(session_id: UUID, question: str) -> DeliberationState:
    """Create a new deliberation and return its initial state."""
    with tracer.start_as_current_span(
        "create_deliberation",
        attributes={"session.id": str(session_id)},
    ):
        row = await db.pool.fetchrow(_INSERT, session_id, question)
        state = _row_to_state(row)
        _deliberations_created.add(1)
        log.info(
            "created deliberation",
            extra={
                "deliberation_id": str(state.deliberation_id),
                "session_id": str(session_id),
            },
        )
        return state


async def get_deliberation(deliberation_id: UUID) -> DeliberationState | None:
    """Return a deliberation by its UUID, or ``None`` if not found."""
    with tracer.start_as_current_span(
        "get_deliberation",
        attributes={"deliberation.id": str(deliberation_id)},
    ):
        row = await db.pool.fetchrow(_SELECT_BY_ID, deliberation_id)
        if row is None:
            return None
        return _row_to_state(row)


async def update_phase(
    deliberation_id: UUID,
    phase: DeliberationPhase,
    output: str,
) -> None:
    """Store *output* under the *phase* key and advance the deliberation.

    The output is stored in ``phase_outputs[phase]`` — i.e., the key matches
    the phase that produced the output.  Only running deliberations can be
    advanced.

    Raises :class:`LookupError` if no running deliberation matches.
    """
    with tracer.start_as_current_span(
        "update_deliberation_phase",
        attributes={
            "deliberation.id": str(deliberation_id),
            "deliberation.phase": phase,
        },
    ):
        result = await db.pool.fetchval(
            _UPDATE_PHASE,
            deliberation_id,
            phase,
            phase,
            output,
        )
        if result is None:
            msg = f"no running deliberation {deliberation_id}"
            raise LookupError(msg)
        _phase_transitions.add(1, {"deliberation.phase": phase})
        log.info(
            "advanced deliberation phase",
            extra={
                "deliberation_id": str(deliberation_id),
                "phase": phase,
            },
        )


async def complete_deliberation(
    deliberation_id: UUID,
    status: Literal["completed", "failed", "cancelled"] = "completed",
) -> None:
    """Mark a running deliberation as finished with the given *status*.

    Raises :class:`LookupError` if no running deliberation matches.
    """
    with tracer.start_as_current_span(
        "complete_deliberation",
        attributes={
            "deliberation.id": str(deliberation_id),
            "deliberation.status": status,
        },
    ):
        result = await db.pool.fetchval(_COMPLETE, deliberation_id, status)
        if result is None:
            msg = f"no running deliberation {deliberation_id}"
            raise LookupError(msg)
        _deliberations_completed.add(1, {"deliberation.status": status})
        log.info(
            "completed deliberation",
            extra={
                "deliberation_id": str(deliberation_id),
                "status": status,
            },
        )


async def mark_delivered(deliberation_id: UUID) -> None:
    """Mark a completed deliberation as delivered to the user.

    Raises :class:`LookupError` if the deliberation is not in a deliverable
    state (completed + not yet delivered).
    """
    with tracer.start_as_current_span(
        "mark_deliberation_delivered",
        attributes={"deliberation.id": str(deliberation_id)},
    ):
        result = await db.pool.fetchval(_MARK_DELIVERED, deliberation_id)
        if result is None:
            msg = f"deliberation {deliberation_id} not deliverable"
            raise LookupError(msg)
        log.info(
            "marked deliberation delivered",
            extra={"deliberation_id": str(deliberation_id)},
        )


async def list_pending_delivery(*, limit: int = 100) -> list[DeliberationState]:
    """Return all completed but undelivered deliberations, oldest first."""
    with tracer.start_as_current_span("list_pending_delivery"):
        rows = await db.pool.fetch(_LIST_PENDING_DELIVERY, limit)
        results = [_row_to_state(r) for r in rows]
        log.debug("found pending deliberations", extra={"count": len(results)})
        return results


async def list_active(session_id: UUID, *, limit: int = 100) -> list[DeliberationState]:
    """Return all running deliberations for a session, oldest first."""
    with tracer.start_as_current_span(
        "list_active_deliberations",
        attributes={"session.id": str(session_id)},
    ):
        rows = await db.pool.fetch(_LIST_ACTIVE, session_id, limit)
        results = [_row_to_state(r) for r in rows]
        log.debug(
            "found active deliberations",
            extra={"count": len(results), "session_id": str(session_id)},
        )
        return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_state(row: Any) -> DeliberationState:
    return DeliberationState(
        id=row["id"],
        deliberation_id=row["deliberation_id"],
        session_id=row["session_id"],
        question=row["question"],
        phase=row["phase"],
        phase_outputs=row["phase_outputs"],
        status=row["status"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        updated_at=row["updated_at"],
        delivered=row["delivered"],
    )
