"""Tests for theo.memory.self_model — domain accuracy tracking."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from theo.errors import SelfModelDomainNotFoundError
from theo.memory._types import DomainResult
from theo.memory.self_model import read_domains, record_outcome

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _domain_row(  # noqa: PLR0913
    *,
    domain_id: int = 1,
    domain: str = "scheduling",
    accuracy: float | None = None,
    total_predictions: int = 0,
    correct_predictions: int = 0,
    last_evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": domain_id,
        "domain": domain,
        "accuracy": accuracy,
        "total_predictions": total_predictions,
        "correct_predictions": correct_predictions,
        "last_evaluated_at": last_evaluated_at,
        "created_at": _NOW,
    }


@pytest.fixture
def mock_pool() -> AsyncMock:
    return AsyncMock()


# ---------------------------------------------------------------------------
# read_domains
# ---------------------------------------------------------------------------


async def test_read_domains_returns_all(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = [
        _domain_row(domain_id=1, domain="drafting"),
        _domain_row(domain_id=2, domain="research"),
        _domain_row(domain_id=3, domain="scheduling"),
    ]

    with patch("theo.memory.self_model.db", pool=mock_pool):
        result = await read_domains()

    assert len(result) == 3
    assert all(isinstance(r, DomainResult) for r in result)
    assert [r.domain for r in result] == ["drafting", "research", "scheduling"]


async def test_read_domains_returns_empty_when_no_rows(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = []

    with patch("theo.memory.self_model.db", pool=mock_pool):
        result = await read_domains()

    assert result == []


async def test_read_domains_preserves_null_accuracy(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = [_domain_row(accuracy=None)]

    with patch("theo.memory.self_model.db", pool=mock_pool):
        result = await read_domains()

    assert result[0].accuracy is None
    assert result[0].total_predictions == 0


# ---------------------------------------------------------------------------
# record_outcome
# ---------------------------------------------------------------------------


async def test_record_outcome_correct(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = _domain_row(
        domain="scheduling",
        accuracy=1.0,
        total_predictions=1,
        correct_predictions=1,
        last_evaluated_at=_NOW,
    )

    with patch("theo.memory.self_model.db", pool=mock_pool):
        result = await record_outcome("scheduling", correct=True)

    assert isinstance(result, DomainResult)
    assert result.domain == "scheduling"
    assert result.accuracy == 1.0
    assert result.total_predictions == 1
    assert result.correct_predictions == 1
    assert result.last_evaluated_at == _NOW

    # Verify SQL args: domain, correct
    call_args = mock_pool.fetchrow.call_args.args
    assert call_args[1] == "scheduling"
    assert call_args[2] is True


async def test_record_outcome_incorrect(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = _domain_row(
        domain="drafting",
        accuracy=0.0,
        total_predictions=1,
        correct_predictions=0,
        last_evaluated_at=_NOW,
    )

    with patch("theo.memory.self_model.db", pool=mock_pool):
        result = await record_outcome("drafting", correct=False)

    assert result.accuracy == 0.0
    assert result.correct_predictions == 0
    assert result.total_predictions == 1

    call_args = mock_pool.fetchrow.call_args.args
    assert call_args[2] is False


async def test_record_outcome_accuracy_calculation(mock_pool: AsyncMock) -> None:
    """After 3 correct and 1 incorrect, accuracy should be 0.75."""
    mock_pool.fetchrow.return_value = _domain_row(
        domain="research",
        accuracy=0.75,
        total_predictions=4,
        correct_predictions=3,
        last_evaluated_at=_NOW,
    )

    with patch("theo.memory.self_model.db", pool=mock_pool):
        result = await record_outcome("research", correct=True)

    assert result.accuracy == pytest.approx(0.75)
    assert result.total_predictions == 4
    assert result.correct_predictions == 3


async def test_record_outcome_unknown_domain_raises(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = None

    with (
        patch("theo.memory.self_model.db", pool=mock_pool),
        pytest.raises(SelfModelDomainNotFoundError, match="unknown self-model domain"),
    ):
        await record_outcome("nonexistent", correct=True)


# ---------------------------------------------------------------------------
# Result type invariants
# ---------------------------------------------------------------------------


def test_domain_result_is_frozen() -> None:
    result = DomainResult(
        id=1,
        domain="scheduling",
        accuracy=None,
        total_predictions=0,
        correct_predictions=0,
        last_evaluated_at=None,
        created_at=_NOW,
    )
    with pytest.raises(AttributeError):
        result.domain = "changed"  # type: ignore[misc]


def test_domain_result_null_accuracy_before_first_measurement() -> None:
    result = DomainResult(
        id=1,
        domain="scheduling",
        accuracy=None,
        total_predictions=0,
        correct_predictions=0,
        last_evaluated_at=None,
        created_at=_NOW,
    )
    assert result.accuracy is None
    assert result.last_evaluated_at is None
