"""Tests for theo.deliberation — deliberation store CRUD operations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from theo.deliberation import (
    DeliberationState,
    complete_deliberation,
    create_deliberation,
    get_deliberation,
    list_active,
    list_pending_delivery,
    mark_delivered,
    update_phase,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 3, 28, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = UUID("00000000-0000-0000-0000-000000000001")
_DELIBERATION_ID = UUID("00000000-0000-0000-0000-000000000099")


def _delib_row(  # noqa: PLR0913
    *,
    row_id: int = 1,
    deliberation_id: UUID = _DELIBERATION_ID,
    session_id: UUID = _SESSION_ID,
    question: str = "What should I focus on this quarter?",
    phase: str = "frame",
    phase_outputs: dict[str, Any] | None = None,
    status: str = "running",
    completed_at: datetime | None = None,
    delivered: bool = False,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "deliberation_id": deliberation_id,
        "session_id": session_id,
        "question": question,
        "phase": phase,
        "phase_outputs": phase_outputs if phase_outputs is not None else {},
        "status": status,
        "created_at": _NOW,
        "completed_at": completed_at,
        "updated_at": _NOW,
        "delivered": delivered,
    }


@pytest.fixture
def mock_pool() -> AsyncMock:
    return AsyncMock()


# ---------------------------------------------------------------------------
# create_deliberation
# ---------------------------------------------------------------------------


async def test_create_deliberation_returns_state(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = _delib_row()

    with patch("theo.deliberation.db", pool=mock_pool):
        result = await create_deliberation(_SESSION_ID, "What should I focus on?")

    assert isinstance(result, DeliberationState)
    assert result.session_id == _SESSION_ID
    assert result.phase == "frame"
    assert result.status == "running"
    assert result.delivered is False


async def test_create_deliberation_passes_params(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = _delib_row()

    with patch("theo.deliberation.db", pool=mock_pool):
        await create_deliberation(_SESSION_ID, "My question")

    args = mock_pool.fetchrow.call_args.args
    assert args[1] == _SESSION_ID
    assert args[2] == "My question"


# ---------------------------------------------------------------------------
# get_deliberation
# ---------------------------------------------------------------------------


async def test_get_deliberation_returns_state(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = _delib_row(phase="gather", phase_outputs={"frame": "done"})

    with patch("theo.deliberation.db", pool=mock_pool):
        result = await get_deliberation(_DELIBERATION_ID)

    assert result is not None
    assert result.phase == "gather"
    assert result.phase_outputs == {"frame": "done"}


async def test_get_deliberation_returns_none_when_missing(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = None

    with patch("theo.deliberation.db", pool=mock_pool):
        result = await get_deliberation(uuid4())

    assert result is None


# ---------------------------------------------------------------------------
# update_phase
# ---------------------------------------------------------------------------


async def test_update_phase_advances_deliberation(mock_pool: AsyncMock) -> None:
    mock_pool.fetchval.return_value = 1  # deliberation id returned

    with patch("theo.deliberation.db", pool=mock_pool):
        await update_phase(_DELIBERATION_ID, "gather", "framing complete")

    args = mock_pool.fetchval.call_args.args
    assert args[1] == _DELIBERATION_ID
    assert args[2] == "gather"  # new phase
    assert args[3] == "gather"  # key in phase_outputs
    assert args[4] == "framing complete"  # output value


async def test_update_phase_raises_on_not_running(mock_pool: AsyncMock) -> None:
    mock_pool.fetchval.return_value = None

    with (
        patch("theo.deliberation.db", pool=mock_pool),
        pytest.raises(LookupError, match="no running deliberation"),
    ):
        await update_phase(_DELIBERATION_ID, "gather", "output")


# ---------------------------------------------------------------------------
# complete_deliberation
# ---------------------------------------------------------------------------


async def test_complete_deliberation_marks_completed(mock_pool: AsyncMock) -> None:
    mock_pool.fetchval.return_value = 1

    with patch("theo.deliberation.db", pool=mock_pool):
        await complete_deliberation(_DELIBERATION_ID)

    args = mock_pool.fetchval.call_args.args
    assert args[1] == _DELIBERATION_ID
    assert args[2] == "completed"


async def test_complete_deliberation_with_failed_status(mock_pool: AsyncMock) -> None:
    mock_pool.fetchval.return_value = 1

    with patch("theo.deliberation.db", pool=mock_pool):
        await complete_deliberation(_DELIBERATION_ID, status="failed")

    args = mock_pool.fetchval.call_args.args
    assert args[2] == "failed"


async def test_complete_deliberation_with_cancelled_status(mock_pool: AsyncMock) -> None:
    mock_pool.fetchval.return_value = 1

    with patch("theo.deliberation.db", pool=mock_pool):
        await complete_deliberation(_DELIBERATION_ID, status="cancelled")

    args = mock_pool.fetchval.call_args.args
    assert args[2] == "cancelled"


async def test_complete_deliberation_raises_on_not_running(mock_pool: AsyncMock) -> None:
    mock_pool.fetchval.return_value = None

    with (
        patch("theo.deliberation.db", pool=mock_pool),
        pytest.raises(LookupError, match="no running deliberation"),
    ):
        await complete_deliberation(_DELIBERATION_ID)


async def test_complete_already_completed_raises(mock_pool: AsyncMock) -> None:
    """Completing a non-running deliberation raises LookupError."""
    mock_pool.fetchval.return_value = None  # WHERE status = 'running' won't match

    with (
        patch("theo.deliberation.db", pool=mock_pool),
        pytest.raises(LookupError),
    ):
        await complete_deliberation(_DELIBERATION_ID)


# ---------------------------------------------------------------------------
# mark_delivered
# ---------------------------------------------------------------------------


async def test_mark_delivered_succeeds(mock_pool: AsyncMock) -> None:
    mock_pool.fetchval.return_value = 1

    with patch("theo.deliberation.db", pool=mock_pool):
        await mark_delivered(_DELIBERATION_ID)

    mock_pool.fetchval.assert_awaited_once()


async def test_mark_delivered_raises_if_not_deliverable(mock_pool: AsyncMock) -> None:
    mock_pool.fetchval.return_value = None

    with (
        patch("theo.deliberation.db", pool=mock_pool),
        pytest.raises(LookupError, match="not deliverable"),
    ):
        await mark_delivered(_DELIBERATION_ID)


# ---------------------------------------------------------------------------
# list_pending_delivery
# ---------------------------------------------------------------------------


async def test_list_pending_delivery_returns_completed_undelivered(
    mock_pool: AsyncMock,
) -> None:
    mock_pool.fetch.return_value = [
        _delib_row(row_id=1, status="completed", completed_at=_NOW),
        _delib_row(row_id=2, status="completed", completed_at=_NOW),
    ]

    with patch("theo.deliberation.db", pool=mock_pool):
        result = await list_pending_delivery()

    assert len(result) == 2
    assert all(isinstance(r, DeliberationState) for r in result)


async def test_list_pending_delivery_returns_empty(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = []

    with patch("theo.deliberation.db", pool=mock_pool):
        result = await list_pending_delivery()

    assert result == []


# ---------------------------------------------------------------------------
# list_active
# ---------------------------------------------------------------------------


async def test_list_active_returns_running_for_session(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = [
        _delib_row(row_id=1, phase="frame"),
        _delib_row(row_id=2, phase="gather"),
    ]

    with patch("theo.deliberation.db", pool=mock_pool):
        result = await list_active(_SESSION_ID)

    assert len(result) == 2
    assert result[0].phase == "frame"
    assert result[1].phase == "gather"


async def test_list_active_passes_session_id(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = []

    with patch("theo.deliberation.db", pool=mock_pool):
        await list_active(_SESSION_ID)

    args = mock_pool.fetch.call_args.args
    assert args[1] == _SESSION_ID


async def test_list_active_returns_empty_for_no_running(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = []

    with patch("theo.deliberation.db", pool=mock_pool):
        result = await list_active(_SESSION_ID)

    assert result == []


# ---------------------------------------------------------------------------
# Result type invariants
# ---------------------------------------------------------------------------


def test_deliberation_state_is_frozen() -> None:
    state = DeliberationState(
        id=1,
        deliberation_id=_DELIBERATION_ID,
        session_id=_SESSION_ID,
        question="test",
        phase="frame",
        phase_outputs={},
        status="running",
        created_at=_NOW,
        completed_at=None,
        updated_at=_NOW,
        delivered=False,
    )
    with pytest.raises(AttributeError):
        state.phase = "gather"  # type: ignore[misc]


def test_deliberation_state_has_slots() -> None:
    state = DeliberationState(
        id=1,
        deliberation_id=_DELIBERATION_ID,
        session_id=_SESSION_ID,
        question="test",
        phase="frame",
        phase_outputs={},
        status="running",
        created_at=_NOW,
        completed_at=None,
        updated_at=_NOW,
        delivered=False,
    )
    assert hasattr(state, "__slots__")
    assert not hasattr(state, "__dict__")
