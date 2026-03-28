"""Core memory operations: read, update, and changelog."""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any, Literal, cast

from opentelemetry import metrics, trace

from theo.db import db

if TYPE_CHECKING:
    from datetime import datetime

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)
_core_updates = _meter.create_counter(
    "theo.memory.core.updates",
    description="Total core memory document updates",
)

type CoreMemoryLabel = Literal["persona", "goals", "user_model", "context"]

_VALID_LABELS: frozenset[str] = frozenset({"persona", "goals", "user_model", "context"})

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class CoreDocument:
    """A single core memory document."""

    label: CoreMemoryLabel
    body: dict[str, Any]
    version: int
    updated_at: datetime


@dataclasses.dataclass(frozen=True, slots=True)
class ChangelogEntry:
    """A single entry from the core memory changelog."""

    id: int
    label: CoreMemoryLabel
    old_body: dict[str, Any]
    new_body: dict[str, Any]
    version: int
    reason: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_SELECT_ALL = """
SELECT label, body, version, updated_at
FROM core_memory
ORDER BY label
"""

_SELECT_ONE = """
SELECT label, body, version, updated_at
FROM core_memory
WHERE label = $1
"""

_UPDATE = """
UPDATE core_memory
SET body = $2, version = version + 1
WHERE label = $1
RETURNING version
"""

_INSERT_LOG = """
INSERT INTO core_memory_log (label, old_body, new_body, version, reason)
VALUES ($1, $2, $3, $4, $5)
"""

_SELECT_LOG = """
SELECT id, label, old_body, new_body, version, reason, created_at
FROM core_memory_log
WHERE label = $1
ORDER BY created_at DESC
LIMIT $2
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _validate_label(label: str) -> CoreMemoryLabel:
    """Raise ``ValueError`` if *label* is not a valid core memory label."""
    if label not in _VALID_LABELS:
        msg = f"invalid core memory label {label!r}, must be one of {sorted(_VALID_LABELS)}"
        raise ValueError(msg)
    return cast("CoreMemoryLabel", label)


async def read_all() -> dict[CoreMemoryLabel, CoreDocument]:
    """Return all core memory documents keyed by label."""
    with tracer.start_as_current_span(
        "read_core_memory",
        attributes={"core_memory.operation": "read_all"},
    ):
        rows = await db.pool.fetch(_SELECT_ALL)
        result: dict[CoreMemoryLabel, CoreDocument] = {}
        for row in rows:
            doc = _row_to_document(row)
            result[doc.label] = doc
        log.debug("read %d core memory document(s)", len(result))
        return result


async def read_one(label: str) -> CoreDocument:
    """Return a single core memory document by *label*.

    Raises ``ValueError`` for invalid labels and ``LookupError`` if the
    document is missing (should never happen with seeded data).
    """
    validated = _validate_label(label)
    with tracer.start_as_current_span(
        "read_core_memory",
        attributes={"core_memory.label": validated, "core_memory.operation": "read_one"},
    ):
        row = await db.pool.fetchrow(_SELECT_ONE, validated)
        if row is None:
            msg = f"core memory document {validated!r} not found"
            raise LookupError(msg)
        return _row_to_document(row)


async def update(label: str, *, body: dict[str, Any], reason: str | None = None) -> int:
    """Update a core memory document and log the change.

    Returns the new version number. The update and log insert run in a
    single transaction so both succeed or neither does.
    """
    validated = _validate_label(label)
    with tracer.start_as_current_span(
        "update_core_memory",
        attributes={"core_memory.label": validated, "core_memory.operation": "update"},
    ):
        async with db.pool.acquire() as conn, conn.transaction():
            old_row = await conn.fetchrow(_SELECT_ONE, validated)
            if old_row is None:
                msg = f"core memory document {validated!r} not found"
                raise LookupError(msg)

            old_body: dict[str, Any] = old_row["body"]
            new_version: int = await conn.fetchval(_UPDATE, validated, body)

            await conn.execute(
                _INSERT_LOG,
                validated,
                old_body,
                body,
                new_version,
                reason,
            )

        _core_updates.add(1, {"core_memory.label": validated})
        log.info(
            "updated core memory",
            extra={"label": validated, "version": new_version, "reason": reason},
        )
        return new_version


async def read_changelog(label: str, *, limit: int = 20) -> list[ChangelogEntry]:
    """Return recent changelog entries for *label*, newest first."""
    validated = _validate_label(label)
    with tracer.start_as_current_span(
        "read_core_memory_changelog",
        attributes={"core_memory.label": validated, "core_memory.operation": "changelog"},
    ):
        rows = await db.pool.fetch(_SELECT_LOG, validated, limit)
        entries = [_row_to_changelog(r) for r in rows]
        log.debug("read %d changelog entry(ies) for %s", len(entries), validated)
        return entries


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_document(row: Any) -> CoreDocument:
    return CoreDocument(
        label=row["label"],
        body=row["body"],
        version=row["version"],
        updated_at=row["updated_at"],
    )


def _row_to_changelog(row: Any) -> ChangelogEntry:
    return ChangelogEntry(
        id=row["id"],
        label=row["label"],
        old_body=row["old_body"],
        new_body=row["new_body"],
        version=row["version"],
        reason=row["reason"],
        created_at=row["created_at"],
    )
