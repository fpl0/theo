"""Tests for theo.memory.nodes — store, search, get operations."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from theo.memory import NodeResult
from theo.memory.nodes import get_node, search_nodes, store_node

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
_DIM = 768


def _fake_vector() -> np.ndarray:
    vec = np.random.default_rng(42).standard_normal(_DIM).astype(np.float32)
    return vec / np.linalg.norm(vec)


def _node_row(  # noqa: PLR0913
    *,
    node_id: int = 1,
    kind: str = "fact",
    body: str = "Python was created by Guido van Rossum",
    trust: str = "inferred",
    confidence: float = 0.8,
    importance: float = 0.7,
    sensitivity: str = "normal",
    similarity: float | None = None,
) -> dict:
    row: dict = {
        "id": node_id,
        "kind": kind,
        "body": body,
        "trust": trust,
        "confidence": confidence,
        "importance": importance,
        "sensitivity": sensitivity,
        "meta": {},
        "created_at": _NOW,
    }
    if similarity is not None:
        row["similarity"] = similarity
    return row


@pytest.fixture
def mock_pool() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_embedder() -> AsyncMock:
    mock = AsyncMock()
    mock.embed_one.return_value = _fake_vector()
    return mock


# ---------------------------------------------------------------------------
# store_node
# ---------------------------------------------------------------------------


async def test_store_inserts_and_returns_id(
    mock_pool: AsyncMock, mock_embedder: AsyncMock
) -> None:
    mock_pool.fetchval.return_value = 42

    with (
        patch("theo.memory.nodes.db", pool=mock_pool),
        patch("theo.memory.nodes.embedder", mock_embedder),
    ):
        result = await store_node(kind="fact", body="Earth orbits the Sun")

    assert result == 42
    mock_embedder.embed_one.assert_awaited_once_with("Earth orbits the Sun")
    mock_pool.fetchval.assert_awaited_once()

    # Verify parametrised args: kind, body, vec, trust, confidence, importance, sensitivity, meta
    args = mock_pool.fetchval.call_args.args
    assert args[1] == "fact"
    assert args[2] == "Earth orbits the Sun"
    assert args[4] == "inferred"  # default trust
    assert args[5] == 0.5  # default confidence
    assert args[6] == 0.5  # default importance
    assert args[7] == "normal"  # default sensitivity
    assert args[8] == {}  # default meta


async def test_store_passes_custom_fields(mock_pool: AsyncMock, mock_embedder: AsyncMock) -> None:
    mock_pool.fetchval.return_value = 7

    with (
        patch("theo.memory.nodes.db", pool=mock_pool),
        patch("theo.memory.nodes.embedder", mock_embedder),
    ):
        result = await store_node(
            kind="person",
            body="Ada Lovelace",
            trust="owner",
            confidence=0.95,
            importance=0.9,
            sensitivity="sensitive",
            meta={"source": "manual"},
        )

    assert result == 7
    args = mock_pool.fetchval.call_args.args
    assert args[1] == "person"
    assert args[4] == "owner"
    assert args[5] == 0.95
    assert args[6] == 0.9
    assert args[7] == "sensitive"
    assert args[8] == {"source": "manual"}


# ---------------------------------------------------------------------------
# get_node
# ---------------------------------------------------------------------------


async def test_get_returns_node_result(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = _node_row(node_id=5)

    with patch("theo.memory.nodes.db", pool=mock_pool):
        result = await get_node(5)

    assert result is not None
    assert isinstance(result, NodeResult)
    assert result.id == 5
    assert result.kind == "fact"
    assert result.trust == "inferred"
    assert result.confidence == 0.8
    assert result.importance == 0.7
    assert result.sensitivity == "normal"
    assert result.created_at == _NOW
    assert result.similarity is None


async def test_get_returns_none_for_missing(mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow.return_value = None

    with patch("theo.memory.nodes.db", pool=mock_pool):
        result = await get_node(999)

    assert result is None
    mock_pool.fetchrow.assert_awaited_once()


# ---------------------------------------------------------------------------
# search_nodes
# ---------------------------------------------------------------------------


async def test_search_returns_ordered_results(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    mock_pool.fetch.return_value = [
        _node_row(node_id=1, body="closest match", similarity=0.95),
        _node_row(node_id=2, body="second match", similarity=0.80),
        _node_row(node_id=3, body="distant match", similarity=0.60),
    ]

    with (
        patch("theo.memory.nodes.db", pool=mock_pool),
        patch("theo.memory.nodes.embedder", mock_embedder),
    ):
        results = await search_nodes("test query", limit=3)

    assert len(results) == 3
    assert all(isinstance(r, NodeResult) for r in results)
    assert results[0].similarity == 0.95
    assert results[1].similarity == 0.80
    assert results[2].similarity == 0.60
    mock_embedder.embed_one.assert_awaited_once_with("test query")


async def test_search_with_kind_filter(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    mock_pool.fetch.return_value = [
        _node_row(node_id=10, kind="person", similarity=0.9),
    ]

    with (
        patch("theo.memory.nodes.db", pool=mock_pool),
        patch("theo.memory.nodes.embedder", mock_embedder),
    ):
        results = await search_nodes("find people", kind="person", limit=5)

    assert len(results) == 1
    assert results[0].kind == "person"

    # Should use the kind-filtered query with 3 positional args: vec, kind, limit
    args = mock_pool.fetch.call_args.args
    assert args[2] == "person"
    assert args[3] == 5


async def test_search_without_kind_uses_unfiltered_query(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    mock_pool.fetch.return_value = []

    with (
        patch("theo.memory.nodes.db", pool=mock_pool),
        patch("theo.memory.nodes.embedder", mock_embedder),
    ):
        results = await search_nodes("anything", limit=10)

    assert results == []
    # Unfiltered query: vec, limit (2 positional args after SQL)
    args = mock_pool.fetch.call_args.args
    assert len(args) == 3  # SQL + vec + limit


async def test_search_default_limit(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    mock_pool.fetch.return_value = []

    with (
        patch("theo.memory.nodes.db", pool=mock_pool),
        patch("theo.memory.nodes.embedder", mock_embedder),
    ):
        await search_nodes("query")

    args = mock_pool.fetch.call_args.args
    assert args[2] == 10  # default limit


# ---------------------------------------------------------------------------
# Result type invariants
# ---------------------------------------------------------------------------


def test_node_result_is_frozen() -> None:
    result = NodeResult(
        id=1,
        kind="fact",
        body="test",
        trust="inferred",
        confidence=0.5,
        importance=0.5,
        sensitivity="normal",
        meta={},
        created_at=_NOW,
    )
    with pytest.raises(AttributeError):
        result.body = "changed"  # type: ignore[misc]
