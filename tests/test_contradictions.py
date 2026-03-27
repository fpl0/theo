"""Tests for theo.memory.contradictions — contradiction detection and resolution."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from theo.llm import StreamDone, TextDelta
from theo.memory.contradictions import ConflictResult, check_contradiction, resolve_contradiction
from theo.memory.nodes import store_node

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _node_result(
    *,
    node_id: int = 1,
    kind: str = "fact",
    body: str = "Alice works at Google",
    similarity: float = 0.85,
    confidence: float = 0.8,
) -> MagicMock:
    node = MagicMock()
    node.id = node_id
    node.kind = kind
    node.body = body
    node.similarity = similarity
    node.confidence = confidence
    return node


async def _fake_stream_contradicts(
    _messages: Any,
    **_kwargs: Any,
) -> AsyncIterator[TextDelta | StreamDone]:
    """Simulate an LLM stream that returns a contradiction JSON."""
    payload = json.dumps({"contradicts": True, "explanation": "Conflicting employers"})
    yield TextDelta(text=payload)
    yield StreamDone(input_tokens=10, output_tokens=20, stop_reason="end_turn")


async def _fake_stream_no_contradiction(
    _messages: Any,
    **_kwargs: Any,
) -> AsyncIterator[TextDelta | StreamDone]:
    """Simulate an LLM stream that returns no contradiction."""
    payload = json.dumps({"contradicts": False, "explanation": ""})
    yield TextDelta(text=payload)
    yield StreamDone(input_tokens=10, output_tokens=20, stop_reason="end_turn")


async def _fake_stream_invalid_json(
    _messages: Any,
    **_kwargs: Any,
) -> AsyncIterator[TextDelta | StreamDone]:
    """Simulate an LLM stream that returns invalid JSON."""
    yield TextDelta(text="I'm not sure about that")
    yield StreamDone(input_tokens=10, output_tokens=20, stop_reason="end_turn")


def _make_pool_with_conn(mock_conn: AsyncMock) -> MagicMock:
    """Create a pool mock whose ``acquire()`` yields *mock_conn* as an async CM."""
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


@pytest.fixture
def mock_embedder() -> AsyncMock:
    mock = AsyncMock()
    vec = np.random.default_rng(42).standard_normal(768).astype(np.float32)
    mock.embed_one.return_value = vec / np.linalg.norm(vec)
    return mock


# ---------------------------------------------------------------------------
# check_contradiction
# ---------------------------------------------------------------------------


async def test_check_contradiction_finds_conflict() -> None:
    candidate = _node_result(node_id=42, similarity=0.85)
    mock_search = AsyncMock(return_value=[candidate])

    with (
        patch("theo.memory.contradictions.search_nodes", mock_search),
        patch("theo.memory.contradictions.stream_response", _fake_stream_contradicts),
        patch("theo.memory.contradictions.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(llm_max_tokens=1024)
        result = await check_contradiction("Alice works at Meta", kind="fact")

    assert result is not None
    assert result.conflicting_node_id == 42
    assert result.confidence_reduction == 0.3
    assert result.explanation == "Conflicting employers"
    mock_search.assert_awaited_once_with("Alice works at Meta", kind="fact", limit=5)


async def test_check_contradiction_returns_none_when_no_conflict() -> None:
    candidate = _node_result(node_id=10, similarity=0.8)
    mock_search = AsyncMock(return_value=[candidate])

    with (
        patch("theo.memory.contradictions.search_nodes", mock_search),
        patch("theo.memory.contradictions.stream_response", _fake_stream_no_contradiction),
        patch("theo.memory.contradictions.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(llm_max_tokens=1024)
        result = await check_contradiction("Alice works at Google", kind="fact")

    assert result is None


async def test_check_contradiction_skips_low_similarity() -> None:
    candidate = _node_result(node_id=5, similarity=0.5)
    mock_search = AsyncMock(return_value=[candidate])

    with patch("theo.memory.contradictions.search_nodes", mock_search):
        result = await check_contradiction("Unrelated statement", kind="fact")

    assert result is None


async def test_check_contradiction_returns_none_when_no_candidates() -> None:
    mock_search = AsyncMock(return_value=[])

    with patch("theo.memory.contradictions.search_nodes", mock_search):
        result = await check_contradiction("New fact", kind="fact")

    assert result is None


async def test_check_contradiction_handles_invalid_llm_json() -> None:
    candidate = _node_result(node_id=7, similarity=0.9)
    mock_search = AsyncMock(return_value=[candidate])

    with (
        patch("theo.memory.contradictions.search_nodes", mock_search),
        patch("theo.memory.contradictions.stream_response", _fake_stream_invalid_json),
        patch("theo.memory.contradictions.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(llm_max_tokens=1024)
        result = await check_contradiction("Some statement", kind="fact")

    assert result is None


# ---------------------------------------------------------------------------
# resolve_contradiction
# ---------------------------------------------------------------------------


async def test_resolve_contradiction_reduces_confidence_and_creates_edge() -> None:
    mock_conn = AsyncMock()
    # Return current confidence values for new_node then conflicting_node
    mock_conn.fetchval.side_effect = [0.8, 0.9]
    pool = _make_pool_with_conn(mock_conn)
    mock_store_edge = AsyncMock(return_value=1)

    conflict = ConflictResult(
        conflicting_node_id=42,
        confidence_reduction=0.3,
        explanation="Conflicting employers",
    )

    with (
        patch("theo.memory.contradictions.db", pool=pool),
        patch("theo.memory.contradictions.store_edge", mock_store_edge),
    ):
        await resolve_contradiction(new_node_id=100, conflict=conflict)

    # Two confidence updates in the transaction
    assert mock_conn.execute.await_count == 2

    # First update: new_node_id=100, confidence=0.8-0.3=0.5
    first_call = mock_conn.execute.call_args_list[0]
    assert first_call.args[1] == 100
    assert first_call.args[2] == pytest.approx(0.5)

    # Second update: conflicting_node_id=42, confidence=0.9-0.3=0.6
    second_call = mock_conn.execute.call_args_list[1]
    assert second_call.args[1] == 42
    assert second_call.args[2] == pytest.approx(0.6)

    # Edge created
    mock_store_edge.assert_awaited_once_with(
        source_id=100,
        target_id=42,
        label="contradicts",
        weight=1.0,
        meta={"explanation": "Conflicting employers"},
    )


async def test_resolve_contradiction_floors_confidence_at_0_1() -> None:
    mock_conn = AsyncMock()
    # Return low confidence values that would go below 0.1
    mock_conn.fetchval.side_effect = [0.2, 0.15]
    pool = _make_pool_with_conn(mock_conn)

    conflict = ConflictResult(
        conflicting_node_id=42,
        confidence_reduction=0.3,
        explanation="Test",
    )

    with (
        patch("theo.memory.contradictions.db", pool=pool),
        patch("theo.memory.contradictions.store_edge", AsyncMock(return_value=1)),
    ):
        await resolve_contradiction(new_node_id=100, conflict=conflict)

    # The SQL uses GREATEST($2, 0.1), so even negative values are floored at DB level.
    # We pass the raw reduced value and let PostgreSQL handle the floor.
    first_call = mock_conn.execute.call_args_list[0]
    assert first_call.args[2] == pytest.approx(-0.1)  # 0.2 - 0.3

    second_call = mock_conn.execute.call_args_list[1]
    assert second_call.args[2] == pytest.approx(-0.15)  # 0.15 - 0.3


# ---------------------------------------------------------------------------
# ConflictResult invariants
# ---------------------------------------------------------------------------


def test_conflict_result_is_frozen() -> None:
    result = ConflictResult(
        conflicting_node_id=1,
        confidence_reduction=0.3,
        explanation="test",
    )
    with pytest.raises(AttributeError):
        result.explanation = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Integration: store_node fire-and-forget
# ---------------------------------------------------------------------------


async def test_store_node_triggers_contradiction_check_when_enabled(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    """Verify store_node creates a background task for contradiction checking."""
    mock_pool.fetchval.return_value = 99

    mock_check = AsyncMock(return_value=None)

    with (
        patch("theo.memory.nodes.db", pool=mock_pool),
        patch("theo.memory.nodes.embedder", mock_embedder),
        patch("theo.memory.nodes.get_settings") as mock_settings,
        patch("theo.memory.contradictions.check_contradiction", mock_check),
    ):
        mock_settings.return_value = MagicMock(contradiction_check_enabled=True)
        result = await store_node(kind="fact", body="Earth is round")
        # Let the fire-and-forget task run
        await asyncio.sleep(0)

    assert result == 99
    mock_check.assert_awaited_once_with("Earth is round", "fact")


async def test_store_node_skips_contradiction_check_when_disabled(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    """Verify store_node does not spawn a task when contradiction check is disabled."""
    mock_pool.fetchval.return_value = 99

    with (
        patch("theo.memory.nodes.db", pool=mock_pool),
        patch("theo.memory.nodes.embedder", mock_embedder),
        patch("theo.memory.nodes.get_settings") as mock_settings,
        patch("theo.memory.contradictions.check_contradiction") as mock_check,
    ):
        mock_settings.return_value = MagicMock(contradiction_check_enabled=False)
        result = await store_node(kind="fact", body="Earth is round")

    assert result == 99
    mock_check.assert_not_awaited()
