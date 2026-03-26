"""Tests for theo.memory.core — read, update, and changelog operations."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import pytest

from theo.memory.core import (
    ChangelogEntry,
    CoreDocument,
    read_all,
    read_changelog,
    read_one,
    update,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _core_row(
    *,
    label: str = "persona",
    body: dict[str, Any] | None = None,
    version: int = 1,
) -> dict[str, Any]:
    return {
        "label": label,
        "body": body if body is not None else {"summary": "Theo is a personal AI agent."},
        "version": version,
        "updated_at": _NOW,
    }


def _log_row(  # noqa: PLR0913
    *,
    log_id: int = 1,
    label: str = "persona",
    old_body: dict[str, Any] | None = None,
    new_body: dict[str, Any] | None = None,
    version: int = 2,
    reason: str | None = "updated persona",
) -> dict[str, Any]:
    return {
        "id": log_id,
        "label": label,
        "old_body": old_body if old_body is not None else {"summary": "old"},
        "new_body": new_body if new_body is not None else {"summary": "new"},
        "version": version,
        "reason": reason,
        "created_at": _NOW,
    }


def _make_pool_with_conn(mock_conn: AsyncMock) -> MagicMock:
    """Create a pool mock whose ``acquire()`` yields *mock_conn* as an async CM.

    Also stubs ``conn.transaction()`` as a no-op async context manager so
    ``async with conn.transaction():`` works correctly.
    """
    pool = AsyncMock()

    @asynccontextmanager
    async def _acquire() -> AsyncIterator[AsyncMock]:
        yield mock_conn

    @asynccontextmanager
    async def _transaction() -> AsyncIterator[None]:
        yield

    pool.acquire = _acquire
    mock_conn.transaction = _transaction
    return pool


@pytest.fixture
def mock_pool() -> AsyncMock:
    return AsyncMock()


# ---------------------------------------------------------------------------
# read_all
# ---------------------------------------------------------------------------


async def test_read_all_returns_all_documents(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = [
        _core_row(label="context"),
        _core_row(label="goals", body={"active": []}),
        _core_row(label="persona"),
        _core_row(label="user_model", body={"preferences": {}}),
    ]

    with patch("theo.memory.core.db", pool=mock_pool):
        result = await read_all()

    assert len(result) == 4
    assert set(result.keys()) == {"persona", "goals", "user_model", "context"}
    assert all(isinstance(v, CoreDocument) for v in result.values())


async def test_read_all_returns_empty_when_no_rows(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = []

    with patch("theo.memory.core.db", pool=mock_pool):
        result = await read_all()

    assert result == {}


# ---------------------------------------------------------------------------
# read_one
# ---------------------------------------------------------------------------


async def test_read_one_returns_document(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = _core_row(label="persona", version=3)

    with patch("theo.memory.core.db", pool=mock_pool):
        result = await read_one("persona")

    assert isinstance(result, CoreDocument)
    assert result.label == "persona"
    assert result.version == 3
    assert result.updated_at == _NOW


async def test_read_one_raises_on_invalid_label() -> None:
    with pytest.raises(ValueError, match="invalid core memory label"):
        await read_one("nonexistent")


async def test_read_one_raises_lookup_error_if_missing(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = None

    with (
        patch("theo.memory.core.db", pool=mock_pool),
        pytest.raises(LookupError, match="not found"),
    ):
        await read_one("persona")


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


async def test_update_writes_document_and_log() -> None:
    old_body = {"summary": "old persona"}
    new_body = {"summary": "new persona"}

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = _core_row(label="persona", body=old_body, version=1)
    mock_conn.fetchval.return_value = 2
    pool = _make_pool_with_conn(mock_conn)

    with patch("theo.memory.core.db", pool=pool):
        version = await update("persona", body=new_body, reason="evolved understanding")

    assert version == 2
    mock_conn.fetchval.assert_awaited_once()
    mock_conn.execute.assert_awaited_once()

    # Verify log insert args: label, old_body, new_body, version, reason
    log_args = mock_conn.execute.call_args.args
    assert log_args[1] == "persona"
    assert log_args[2] == old_body
    assert log_args[3] == new_body
    assert log_args[4] == 2
    assert log_args[5] == "evolved understanding"


async def test_update_raises_on_invalid_label() -> None:
    with pytest.raises(ValueError, match="invalid core memory label"):
        await update("invalid", body={"key": "val"})


async def test_update_raises_lookup_error_if_missing() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None
    pool = _make_pool_with_conn(mock_conn)

    with (
        patch("theo.memory.core.db", pool=pool),
        pytest.raises(LookupError, match="not found"),
    ):
        await update("persona", body={"new": True})


async def test_update_increments_version() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = _core_row(version=5)
    mock_conn.fetchval.return_value = 6
    pool = _make_pool_with_conn(mock_conn)

    with patch("theo.memory.core.db", pool=pool):
        version = await update("persona", body={"updated": True})

    assert version == 6


async def test_update_uses_transaction() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = _core_row()
    mock_conn.fetchval.return_value = 2
    pool = _make_pool_with_conn(mock_conn)

    with patch("theo.memory.core.db", pool=pool):
        await update("persona", body={"new": True})

    # Both read and write happened on the same connection (inside transaction)
    mock_conn.fetchrow.assert_awaited_once()
    mock_conn.fetchval.assert_awaited_once()
    mock_conn.execute.assert_awaited_once()


async def test_update_accepts_none_reason() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = _core_row()
    mock_conn.fetchval.return_value = 2
    pool = _make_pool_with_conn(mock_conn)

    with patch("theo.memory.core.db", pool=pool):
        version = await update("persona", body={"minimal": True})

    assert version == 2
    log_args = mock_conn.execute.call_args.args
    assert log_args[5] is None  # reason


# ---------------------------------------------------------------------------
# read_changelog
# ---------------------------------------------------------------------------


async def test_read_changelog_returns_entries(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = [
        _log_row(log_id=2, version=3, reason="second update"),
        _log_row(log_id=1, version=2, reason="first update"),
    ]

    with patch("theo.memory.core.db", pool=mock_pool):
        result = await read_changelog("persona")

    assert len(result) == 2
    assert all(isinstance(e, ChangelogEntry) for e in result)
    assert result[0].id == 2
    assert result[0].version == 3
    assert result[1].id == 1


async def test_read_changelog_raises_on_invalid_label() -> None:
    with pytest.raises(ValueError, match="invalid core memory label"):
        await read_changelog("bogus")


async def test_read_changelog_respects_limit(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = []

    with patch("theo.memory.core.db", pool=mock_pool):
        await read_changelog("goals", limit=5)

    args = mock_pool.fetch.call_args.args
    assert args[2] == 5  # limit parameter


async def test_read_changelog_default_limit(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = []

    with patch("theo.memory.core.db", pool=mock_pool):
        await read_changelog("goals")

    args = mock_pool.fetch.call_args.args
    assert args[2] == 20  # default limit


# ---------------------------------------------------------------------------
# Validation — all four valid labels accepted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["persona", "goals", "user_model", "context"])
async def test_read_one_accepts_all_valid_labels(mock_pool: AsyncMock, label: str) -> None:
    mock_pool.fetchrow.return_value = _core_row(label=label)

    with patch("theo.memory.core.db", pool=mock_pool):
        result = await read_one(label)

    assert result.label == label


@pytest.mark.parametrize("label", ["", "system", "memory", "PERSONA", "Persona"])
async def test_invalid_labels_rejected(label: str) -> None:
    with pytest.raises(ValueError, match="invalid core memory label"):
        await read_one(label)


# ---------------------------------------------------------------------------
# Result type invariants
# ---------------------------------------------------------------------------


def test_core_document_is_frozen() -> None:
    doc = CoreDocument(
        label="persona",
        body={"summary": "test"},
        version=1,
        updated_at=_NOW,
    )
    with pytest.raises(AttributeError):
        doc.body = {"changed": True}  # type: ignore[misc]


def test_changelog_entry_is_frozen() -> None:
    entry = ChangelogEntry(
        id=1,
        label="persona",
        old_body={"old": True},
        new_body={"new": True},
        version=2,
        reason="test",
        created_at=_NOW,
    )
    with pytest.raises(AttributeError):
        entry.reason = "changed"  # type: ignore[misc]
