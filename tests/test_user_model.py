"""Tests for theo.memory.user_model — structured user model CRUD and tool."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from theo.memory._types import DimensionResult
from theo.memory.tools import TOOL_DEFINITIONS, execute_tool
from theo.memory.user_model import get_dimension, read_dimensions, update_dimension

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _dim_row(
    *,
    framework: str = "big_five",
    dimension: str = "openness",
    value: dict[str, Any] | None = None,
    confidence: float = 0.0,
    evidence_count: int = 0,
) -> dict[str, Any]:
    return {
        "id": 1,
        "framework": framework,
        "dimension": dimension,
        "value": value if value is not None else {},
        "confidence": confidence,
        "evidence_count": evidence_count,
        "meta": {},
        "updated_at": _NOW,
    }


@pytest.fixture
def mock_pool() -> AsyncMock:
    return AsyncMock()


# ---------------------------------------------------------------------------
# read_dimensions
# ---------------------------------------------------------------------------


class TestReadDimensions:
    async def test_returns_all_dimensions(self, mock_pool: AsyncMock) -> None:
        mock_pool.fetch.return_value = [
            _dim_row(framework="big_five", dimension="openness"),
            _dim_row(framework="schwartz", dimension="power"),
        ]

        with patch("theo.memory.user_model.db", pool=mock_pool):
            result = await read_dimensions()

        assert len(result) == 2
        assert all(isinstance(r, DimensionResult) for r in result)

    async def test_filters_by_framework(self, mock_pool: AsyncMock) -> None:
        mock_pool.fetch.return_value = [
            _dim_row(framework="big_five", dimension="openness"),
        ]

        with patch("theo.memory.user_model.db", pool=mock_pool):
            result = await read_dimensions(framework="big_five")

        assert len(result) == 1
        assert result[0].framework == "big_five"
        # Verify the framework parameter was passed to the query
        mock_pool.fetch.assert_awaited_once()
        args = mock_pool.fetch.call_args.args
        assert args[1] == "big_five"

    async def test_returns_empty_list_when_no_rows(self, mock_pool: AsyncMock) -> None:
        mock_pool.fetch.return_value = []

        with patch("theo.memory.user_model.db", pool=mock_pool):
            result = await read_dimensions()

        assert result == []


# ---------------------------------------------------------------------------
# get_dimension
# ---------------------------------------------------------------------------


class TestGetDimension:
    async def test_returns_dimension(self, mock_pool: AsyncMock) -> None:
        mock_pool.fetchrow.return_value = _dim_row(
            framework="communication",
            dimension="verbosity",
            value={"level": "concise"},
            confidence=0.3,
            evidence_count=3,
        )

        with patch("theo.memory.user_model.db", pool=mock_pool):
            result = await get_dimension("communication", "verbosity")

        assert result is not None
        assert result.framework == "communication"
        assert result.dimension == "verbosity"
        assert result.value == {"level": "concise"}
        assert result.confidence == pytest.approx(0.3)
        assert result.evidence_count == 3

    async def test_returns_none_when_not_found(self, mock_pool: AsyncMock) -> None:
        mock_pool.fetchrow.return_value = None

        with patch("theo.memory.user_model.db", pool=mock_pool):
            result = await get_dimension("unknown", "missing")

        assert result is None


# ---------------------------------------------------------------------------
# update_dimension
# ---------------------------------------------------------------------------


class TestUpdateDimension:
    async def test_updates_and_returns_result(self, mock_pool: AsyncMock) -> None:
        mock_pool.fetchrow.return_value = _dim_row(
            framework="big_five",
            dimension="openness",
            value={"score": 0.8},
            confidence=0.1,
            evidence_count=1,
        )

        with patch("theo.memory.user_model.db", pool=mock_pool):
            result = await update_dimension(
                "big_five",
                "openness",
                value={"score": 0.8},
                reason="user expressed curiosity",
            )

        assert isinstance(result, DimensionResult)
        assert result.value == {"score": 0.8}
        assert result.evidence_count == 1

    async def test_raises_lookup_error_when_not_found(self, mock_pool: AsyncMock) -> None:
        mock_pool.fetchrow.return_value = None

        with (
            patch("theo.memory.user_model.db", pool=mock_pool),
            pytest.raises(LookupError, match="not found"),
        ):
            await update_dimension("unknown", "missing", value={"x": 1})

    async def test_confidence_ramp_over_10_updates(self, mock_pool: AsyncMock) -> None:
        """Confidence should ramp from 0.0 to 1.0 over 10 evidence points."""
        expected_confidences = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]

        for i, expected_conf in enumerate(expected_confidences, start=1):
            mock_pool.fetchrow.return_value = _dim_row(
                confidence=expected_conf,
                evidence_count=i,
            )

            with patch("theo.memory.user_model.db", pool=mock_pool):
                result = await update_dimension("big_five", "openness", value={"score": 0.8})

            assert result.confidence == pytest.approx(expected_conf)
            assert result.evidence_count == i

    async def test_confidence_caps_at_1(self, mock_pool: AsyncMock) -> None:
        """After 10+ evidence points, confidence stays at 1.0."""
        mock_pool.fetchrow.return_value = _dim_row(
            confidence=1.0,
            evidence_count=15,
        )

        with patch("theo.memory.user_model.db", pool=mock_pool):
            result = await update_dimension("big_five", "openness", value={"score": 0.9})

        assert result.confidence == pytest.approx(1.0)

    async def test_accepts_none_reason(self, mock_pool: AsyncMock) -> None:
        mock_pool.fetchrow.return_value = _dim_row(evidence_count=1, confidence=0.1)

        with patch("theo.memory.user_model.db", pool=mock_pool):
            result = await update_dimension("big_five", "openness", value={"score": 0.5})

        assert isinstance(result, DimensionResult)


# ---------------------------------------------------------------------------
# Tool definition tests
# ---------------------------------------------------------------------------


class TestUpdateUserModelTool:
    def test_tool_defined(self) -> None:
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert "update_user_model" in names

    def test_tool_has_required_fields(self) -> None:
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "update_user_model")
        assert "description" in tool
        assert "input_schema" in tool
        assert tool["input_schema"]["type"] == "object"

    def test_tool_requires_framework_dimension_value(self) -> None:
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "update_user_model")
        assert set(tool["input_schema"]["required"]) == {"framework", "dimension", "value"}

    def test_framework_enum_has_all_7(self) -> None:
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "update_user_model")
        framework_prop = tool["input_schema"]["properties"]["framework"]
        assert set(framework_prop["enum"]) == {
            "schwartz",
            "big_five",
            "narrative",
            "communication",
            "energy",
            "goals",
            "boundaries",
        }

    def test_all_six_tools_defined(self) -> None:
        names = {t["name"] for t in TOOL_DEFINITIONS}
        expected = {
            "store_memory",
            "search_memory",
            "read_core_memory",
            "link_memories",
            "update_core_memory",
            "update_user_model",
        }
        assert names == expected


# ---------------------------------------------------------------------------
# Tool execution tests
# ---------------------------------------------------------------------------


class TestUpdateUserModelExecution:
    async def test_dispatches_correctly(self) -> None:
        mock = AsyncMock(
            return_value=DimensionResult(
                id=1,
                framework="big_five",
                dimension="openness",
                value={"score": 0.8},
                confidence=0.1,
                evidence_count=1,
                meta={},
                updated_at=_NOW,
            ),
        )
        with patch("theo.memory.tools.user_model.update_dimension", mock):
            result = await execute_tool(
                "update_user_model",
                {
                    "framework": "big_five",
                    "dimension": "openness",
                    "value": {"score": 0.8},
                    "reason": "expressed curiosity",
                },
            )

        parsed = json.loads(result)
        assert parsed["updated"] is True
        assert parsed["framework"] == "big_five"
        assert parsed["dimension"] == "openness"
        assert parsed["confidence"] == pytest.approx(0.1)
        assert parsed["evidence_count"] == 1

        mock.assert_awaited_once_with(
            "big_five",
            "openness",
            value={"score": 0.8},
            reason="expressed curiosity",
        )

    async def test_rejects_non_dict_value(self) -> None:
        result = await execute_tool(
            "update_user_model",
            {"framework": "big_five", "dimension": "openness", "value": "not a dict"},
        )
        assert "Error" in result

    async def test_without_reason(self) -> None:
        mock = AsyncMock(
            return_value=DimensionResult(
                id=1,
                framework="goals",
                dimension="active_goals",
                value={"items": ["learn rust"]},
                confidence=0.1,
                evidence_count=1,
                meta={},
                updated_at=_NOW,
            ),
        )
        with patch("theo.memory.tools.user_model.update_dimension", mock):
            await execute_tool(
                "update_user_model",
                {
                    "framework": "goals",
                    "dimension": "active_goals",
                    "value": {"items": ["learn rust"]},
                },
            )

        assert mock.await_args is not None
        _, kwargs = mock.await_args
        assert kwargs["reason"] is None

    async def test_error_returns_string(self) -> None:
        with patch(
            "theo.memory.tools.user_model.update_dimension",
            AsyncMock(side_effect=LookupError("not found")),
        ):
            result = await execute_tool(
                "update_user_model",
                {"framework": "x", "dimension": "y", "value": {"a": 1}},
            )

        assert "Error executing update_user_model" in result
        assert "not found" in result


# ---------------------------------------------------------------------------
# Result type invariants
# ---------------------------------------------------------------------------


class TestDimensionResult:
    def test_is_frozen(self) -> None:
        dim = DimensionResult(
            id=1,
            framework="big_five",
            dimension="openness",
            value={},
            confidence=0.0,
            evidence_count=0,
            meta={},
            updated_at=_NOW,
        )
        with pytest.raises(AttributeError):
            dim.value = {"changed": True}  # type: ignore[misc]

    def test_fields(self) -> None:
        dim = DimensionResult(
            id=42,
            framework="schwartz",
            dimension="universalism",
            value={"score": 0.9},
            confidence=0.5,
            evidence_count=5,
            meta={},
            updated_at=_NOW,
        )
        assert dim.id == 42
        assert dim.framework == "schwartz"
        assert dim.dimension == "universalism"
        assert dim.value == {"score": 0.9}
        assert dim.confidence == 0.5
        assert dim.evidence_count == 5
        assert dim.updated_at == _NOW
