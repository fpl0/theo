"""Tests for theo.autonomy — autonomy classification and action logging."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from theo.autonomy import (
    ActionLogEntry,
    Classification,
    action_type_for_tool,
    classify,
    classify_tool,
    count_consecutive_executed,
    log_action,
    parse_owner_overrides,
    requires_approval,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SESSION_ID = UUID("00000000-0000-0000-0000-000000000001")


def _action_log_row(  # noqa: PLR0913
    *,
    row_id: int = 1,
    action_type: str = "memory_store",
    autonomy_level: str = "autonomous",
    decision: str = "executed",
    context: dict[str, Any] | None = None,
    session_id: UUID | None = _SESSION_ID,
    intent_id: int | None = None,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "action_type": action_type,
        "autonomy_level": autonomy_level,
        "decision": decision,
        "context": json.dumps(context) if context else "{}",
        "session_id": session_id,
        "intent_id": intent_id,
    }


@pytest.fixture
def mock_pool() -> AsyncMock:
    return AsyncMock()


# ---------------------------------------------------------------------------
# classify — default registry
# ---------------------------------------------------------------------------


def test_classify_memory_store_is_autonomous() -> None:
    result = classify("memory_store")
    assert result.autonomy_level == "autonomous"
    assert result.action_type == "memory_store"
    assert result.reason == "default registry"


def test_classify_memory_search_is_autonomous() -> None:
    assert classify("memory_search").autonomy_level == "autonomous"


def test_classify_core_memory_update_is_inform() -> None:
    assert classify("core_memory_update").autonomy_level == "inform"


def test_classify_contradiction_resolve_is_inform() -> None:
    assert classify("contradiction_resolve").autonomy_level == "inform"


def test_classify_deliberation_start_is_autonomous() -> None:
    assert classify("deliberation_start").autonomy_level == "autonomous"


def test_classify_plan_create_is_propose() -> None:
    assert classify("plan_create").autonomy_level == "propose"


def test_classify_plan_execute_step_is_propose() -> None:
    assert classify("plan_execute_step").autonomy_level == "propose"


def test_classify_external_action_is_propose() -> None:
    assert classify("external_action").autonomy_level == "propose"


def test_classify_unknown_action_defaults_to_propose() -> None:
    result = classify("totally_unknown_action")
    assert result.autonomy_level == "propose"
    assert "unknown" in result.reason


# ---------------------------------------------------------------------------
# classify — owner overrides
# ---------------------------------------------------------------------------


def test_classify_with_owner_override() -> None:
    overrides = {"memory_store": "propose"}
    result = classify("memory_store", owner_overrides=overrides)
    assert result.autonomy_level == "propose"
    assert result.reason == "owner override"


def test_classify_override_does_not_affect_other_types() -> None:
    overrides = {"memory_store": "propose"}
    result = classify("memory_search", owner_overrides=overrides)
    assert result.autonomy_level == "autonomous"


def test_classify_empty_overrides_uses_default() -> None:
    result = classify("memory_store", owner_overrides={})
    assert result.autonomy_level == "autonomous"


# ---------------------------------------------------------------------------
# classify_tool
# ---------------------------------------------------------------------------


def test_classify_tool_store_memory() -> None:
    result = classify_tool("store_memory")
    assert result.action_type == "memory_store"
    assert result.autonomy_level == "autonomous"


def test_classify_tool_update_core_memory() -> None:
    result = classify_tool("update_core_memory")
    assert result.action_type == "core_memory_update"
    assert result.autonomy_level == "inform"


def test_classify_tool_unknown_is_external_action() -> None:
    result = classify_tool("send_email")
    assert result.action_type == "external_action"
    assert result.autonomy_level == "propose"


def test_classify_tool_with_override() -> None:
    overrides = {"memory_store": "inform"}
    result = classify_tool("store_memory", owner_overrides=overrides)
    assert result.autonomy_level == "inform"


# ---------------------------------------------------------------------------
# requires_approval
# ---------------------------------------------------------------------------


def test_requires_approval_propose() -> None:
    assert requires_approval("propose") is True


def test_requires_approval_consult() -> None:
    assert requires_approval("consult") is True


def test_requires_approval_autonomous() -> None:
    assert requires_approval("autonomous") is False


def test_requires_approval_inform() -> None:
    assert requires_approval("inform") is False


# ---------------------------------------------------------------------------
# action_type_for_tool
# ---------------------------------------------------------------------------


def test_action_type_for_known_tool() -> None:
    assert action_type_for_tool("store_memory") == "memory_store"
    assert action_type_for_tool("search_memory") == "memory_search"
    assert action_type_for_tool("update_core_memory") == "core_memory_update"


def test_action_type_for_unknown_tool() -> None:
    assert action_type_for_tool("unknown_tool") == "external_action"


# ---------------------------------------------------------------------------
# parse_owner_overrides
# ---------------------------------------------------------------------------


def test_parse_owner_overrides_valid() -> None:
    ctx = {"autonomy_overrides": {"core_memory_update": "propose", "memory_store": "inform"}}
    result = parse_owner_overrides(ctx)
    assert result == {"core_memory_update": "propose", "memory_store": "inform"}


def test_parse_owner_overrides_filters_invalid_levels() -> None:
    ctx = {"autonomy_overrides": {"memory_store": "invalid_level"}}
    result = parse_owner_overrides(ctx)
    assert result == {}


def test_parse_owner_overrides_none_context() -> None:
    assert parse_owner_overrides(None) == {}


def test_parse_owner_overrides_missing_key() -> None:
    assert parse_owner_overrides({"other_key": "value"}) == {}


def test_parse_owner_overrides_not_dict_value() -> None:
    ctx = {"autonomy_overrides": "not a dict"}
    assert parse_owner_overrides(ctx) == {}


# ---------------------------------------------------------------------------
# log_action
# ---------------------------------------------------------------------------


async def test_log_action_inserts_and_returns_entry(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = _action_log_row()

    with patch("theo.autonomy.db", pool=mock_pool):
        entry = await log_action(
            "memory_store",
            "autonomous",
            "executed",
            session_id=_SESSION_ID,
        )

    assert isinstance(entry, ActionLogEntry)
    assert entry.action_type == "memory_store"
    assert entry.autonomy_level == "autonomous"
    assert entry.decision == "executed"
    assert entry.session_id == _SESSION_ID


async def test_log_action_passes_context(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = _action_log_row(context={"tool": "store_memory"})

    with patch("theo.autonomy.db", pool=mock_pool):
        entry = await log_action(
            "memory_store",
            "autonomous",
            "executed",
            context={"tool": "store_memory"},
            session_id=_SESSION_ID,
        )

    assert entry.context == {"tool": "store_memory"}
    args = mock_pool.fetchrow.call_args.args
    assert json.loads(args[4]) == {"tool": "store_memory"}


async def test_log_action_with_intent_id(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = _action_log_row(intent_id=42)

    with patch("theo.autonomy.db", pool=mock_pool):
        entry = await log_action(
            "memory_store",
            "autonomous",
            "executed",
            intent_id=42,
        )

    assert entry.intent_id == 42
    args = mock_pool.fetchrow.call_args.args
    assert args[6] == 42


async def test_log_action_null_session_and_intent(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = _action_log_row(session_id=None, intent_id=None)

    with patch("theo.autonomy.db", pool=mock_pool):
        entry = await log_action("memory_store", "autonomous", "executed")

    assert entry.session_id is None
    assert entry.intent_id is None


# ---------------------------------------------------------------------------
# count_consecutive_executed
# ---------------------------------------------------------------------------


async def test_count_consecutive_returns_count(mock_pool: AsyncMock) -> None:
    mock_pool.fetchval.return_value = 7

    with patch("theo.autonomy.db", pool=mock_pool):
        count = await count_consecutive_executed("memory_store")

    assert count == 7
    args = mock_pool.fetchval.call_args.args
    assert args[1] == "memory_store"
    assert args[2] == 10  # default window


async def test_count_consecutive_custom_window(mock_pool: AsyncMock) -> None:
    mock_pool.fetchval.return_value = 3

    with patch("theo.autonomy.db", pool=mock_pool):
        await count_consecutive_executed("memory_store", window=5)

    args = mock_pool.fetchval.call_args.args
    assert args[2] == 5


async def test_count_consecutive_returns_zero_on_none(mock_pool: AsyncMock) -> None:
    mock_pool.fetchval.return_value = None

    with patch("theo.autonomy.db", pool=mock_pool):
        count = await count_consecutive_executed("memory_store")

    assert count == 0


# ---------------------------------------------------------------------------
# Result type invariants
# ---------------------------------------------------------------------------


def test_classification_is_frozen() -> None:
    c = Classification(action_type="memory_store", autonomy_level="autonomous", reason="test")
    with pytest.raises(AttributeError):
        c.autonomy_level = "propose"  # type: ignore[misc]


def test_classification_has_slots() -> None:
    c = Classification(action_type="memory_store", autonomy_level="autonomous", reason="test")
    assert hasattr(c, "__slots__")
    assert not hasattr(c, "__dict__")


def test_action_log_entry_is_frozen() -> None:
    e = ActionLogEntry(
        id=1,
        action_type="memory_store",
        autonomy_level="autonomous",
        decision="executed",
        context={},
        session_id=None,
        intent_id=None,
    )
    with pytest.raises(AttributeError):
        e.decision = "approved"  # type: ignore[misc]


def test_action_log_entry_has_slots() -> None:
    e = ActionLogEntry(
        id=1,
        action_type="memory_store",
        autonomy_level="autonomous",
        decision="executed",
        context={},
        session_id=None,
        intent_id=None,
    )
    assert hasattr(e, "__slots__")
    assert not hasattr(e, "__dict__")
