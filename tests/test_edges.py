"""Tests for theo.memory.edges — store, get, traverse, expire operations."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import pytest

from theo.memory import EdgeResult, TraversalResult
from theo.memory.edges import expire_edge, get_edges, store_edge, traverse

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _edge_row(
    *,
    edge_id: int = 1,
    source_id: int = 10,
    target_id: int = 20,
    label: str = "related_to",
    weight: float = 1.0,
) -> dict[str, Any]:
    return {
        "id": edge_id,
        "source_id": source_id,
        "target_id": target_id,
        "label": label,
        "weight": weight,
        "meta": {},
        "valid_from": _NOW,
        "valid_to": None,
        "created_at": _NOW,
    }


def _traversal_row(
    *,
    node_id: int = 20,
    depth: int = 1,
    path: list[int] | None = None,
    cumulative_weight: float = 1.0,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "depth": depth,
        "path": path if path is not None else [10, node_id],
        "cumulative_weight": cumulative_weight,
    }


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


# ---------------------------------------------------------------------------
# store_edge
# ---------------------------------------------------------------------------


async def test_store_edge_creates_and_returns_id() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = 42
    pool = _make_pool_with_conn(mock_conn)

    with patch("theo.memory.edges.db", pool=pool):
        result = await store_edge(source_id=10, target_id=20, label="related_to")

    assert result == 42
    # Expire was called first, then insert
    mock_conn.execute.assert_awaited_once()
    mock_conn.fetchval.assert_awaited_once()

    # Verify insert args: source_id, target_id, label, weight, meta
    insert_args = mock_conn.fetchval.call_args.args
    assert insert_args[1] == 10  # source_id
    assert insert_args[2] == 20  # target_id
    assert insert_args[3] == "related_to"  # label
    assert insert_args[4] == 1.0  # default weight
    assert insert_args[5] == {}  # default meta


async def test_store_edge_with_custom_weight_and_meta() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = 7
    pool = _make_pool_with_conn(mock_conn)

    with patch("theo.memory.edges.db", pool=pool):
        result = await store_edge(
            source_id=1,
            target_id=2,
            label="co_occurred",
            weight=0.75,
            meta={"episode_id": 99},
        )

    assert result == 7
    insert_args = mock_conn.fetchval.call_args.args
    assert insert_args[4] == 0.75
    assert insert_args[5] == {"episode_id": 99}


async def test_store_edge_expires_existing_before_insert() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = 2
    pool = _make_pool_with_conn(mock_conn)

    with patch("theo.memory.edges.db", pool=pool):
        await store_edge(source_id=10, target_id=20, label="related_to", weight=0.5)

    # Expire call: source_id, target_id, label
    expire_args = mock_conn.execute.call_args.args
    assert expire_args[1] == 10
    assert expire_args[2] == 20
    assert expire_args[3] == "related_to"


async def test_store_edge_uses_transaction() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = 1
    pool = _make_pool_with_conn(mock_conn)

    with patch("theo.memory.edges.db", pool=pool):
        await store_edge(source_id=1, target_id=2, label="test")

    # Both expire and insert happened on the same connection (inside transaction)
    mock_conn.execute.assert_awaited_once()
    mock_conn.fetchval.assert_awaited_once()


# ---------------------------------------------------------------------------
# get_edges
# ---------------------------------------------------------------------------


async def test_get_edges_outgoing(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = [
        _edge_row(edge_id=1, source_id=10, target_id=20),
        _edge_row(edge_id=2, source_id=10, target_id=30),
    ]

    with patch("theo.memory.edges.db", pool=mock_pool):
        results = await get_edges(10, direction="outgoing")

    assert len(results) == 2
    assert all(isinstance(r, EdgeResult) for r in results)
    assert results[0].id == 1
    assert results[1].id == 2

    # Verify query used source_id
    args = mock_pool.fetch.call_args.args
    assert args[1] == 10


async def test_get_edges_incoming(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = [
        _edge_row(edge_id=3, source_id=5, target_id=10),
    ]

    with patch("theo.memory.edges.db", pool=mock_pool):
        results = await get_edges(10, direction="incoming")

    assert len(results) == 1
    assert results[0].source_id == 5
    assert results[0].target_id == 10


async def test_get_edges_both_directions(mock_pool: AsyncMock) -> None:
    outgoing_row = _edge_row(edge_id=1, source_id=10, target_id=20)
    incoming_row = _edge_row(edge_id=2, source_id=5, target_id=10)
    mock_pool.fetch.side_effect = [[outgoing_row], [incoming_row]]

    with patch("theo.memory.edges.db", pool=mock_pool):
        results = await get_edges(10, direction="both")

    assert len(results) == 2
    assert mock_pool.fetch.await_count == 2


async def test_get_edges_with_label_filter(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = [
        _edge_row(edge_id=1, label="co_occurred"),
    ]

    with patch("theo.memory.edges.db", pool=mock_pool):
        results = await get_edges(10, direction="outgoing", label="co_occurred")

    assert len(results) == 1
    assert results[0].label == "co_occurred"

    # Verify label was passed as parameter
    args = mock_pool.fetch.call_args.args
    assert args[1] == 10
    assert args[2] == "co_occurred"


async def test_get_edges_empty(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = []

    with patch("theo.memory.edges.db", pool=mock_pool):
        results = await get_edges(999, direction="outgoing")

    assert results == []


# ---------------------------------------------------------------------------
# traverse
# ---------------------------------------------------------------------------


async def test_traverse_returns_reachable_nodes(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = [
        _traversal_row(node_id=20, depth=1, path=[10, 20], cumulative_weight=0.9),
        _traversal_row(node_id=30, depth=2, path=[10, 20, 30], cumulative_weight=0.7),
    ]

    with patch("theo.memory.edges.db", pool=mock_pool):
        results = await traverse(10, max_depth=2)

    assert len(results) == 2
    assert all(isinstance(r, TraversalResult) for r in results)
    # Sorted by cumulative_weight descending
    assert results[0].cumulative_weight >= results[1].cumulative_weight
    assert results[0].node_id == 20
    assert results[1].node_id == 30


async def test_traverse_with_label_filter(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = [
        _traversal_row(node_id=20, depth=1),
    ]

    with patch("theo.memory.edges.db", pool=mock_pool):
        results = await traverse(10, max_depth=3, label="related_to")

    assert len(results) == 1
    # Verify label was passed as parameter
    args = mock_pool.fetch.call_args.args
    assert args[1] == 10
    assert args[2] == 3
    assert args[3] == "related_to"


async def test_traverse_empty_when_no_edges(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = []

    with patch("theo.memory.edges.db", pool=mock_pool):
        results = await traverse(10)

    assert results == []


async def test_traverse_respects_max_depth(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = []

    with patch("theo.memory.edges.db", pool=mock_pool):
        await traverse(10, max_depth=5)

    args = mock_pool.fetch.call_args.args
    assert args[2] == 5


async def test_traverse_orders_by_cumulative_weight(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = [
        _traversal_row(node_id=30, depth=2, cumulative_weight=0.5),
        _traversal_row(node_id=20, depth=1, cumulative_weight=0.9),
    ]

    with patch("theo.memory.edges.db", pool=mock_pool):
        results = await traverse(10)

    assert results[0].node_id == 20
    assert results[0].cumulative_weight == 0.9
    assert results[1].node_id == 30
    assert results[1].cumulative_weight == 0.5


# ---------------------------------------------------------------------------
# expire_edge
# ---------------------------------------------------------------------------


async def test_expire_edge_returns_true_when_updated(mock_pool: AsyncMock) -> None:
    mock_pool.execute.return_value = "UPDATE 1"

    with patch("theo.memory.edges.db", pool=mock_pool):
        result = await expire_edge(42)

    assert result is True
    args = mock_pool.execute.call_args.args
    assert args[1] == 42


async def test_expire_edge_returns_false_when_already_expired(mock_pool: AsyncMock) -> None:
    mock_pool.execute.return_value = "UPDATE 0"

    with patch("theo.memory.edges.db", pool=mock_pool):
        result = await expire_edge(42)

    assert result is False


async def test_expire_edge_returns_false_when_not_found(mock_pool: AsyncMock) -> None:
    mock_pool.execute.return_value = "UPDATE 0"

    with patch("theo.memory.edges.db", pool=mock_pool):
        result = await expire_edge(999)

    assert result is False


# ---------------------------------------------------------------------------
# Result type invariants
# ---------------------------------------------------------------------------


def test_edge_result_is_frozen() -> None:
    edge = EdgeResult(
        id=1,
        source_id=10,
        target_id=20,
        label="related_to",
        weight=1.0,
        meta={},
        valid_from=_NOW,
        valid_to=None,
        created_at=_NOW,
    )
    with pytest.raises(AttributeError):
        edge.label = "changed"  # type: ignore[misc]


def test_traversal_result_is_frozen() -> None:
    tr = TraversalResult(
        node_id=20,
        depth=1,
        path=[10, 20],
        cumulative_weight=0.9,
    )
    with pytest.raises(AttributeError):
        tr.depth = 5  # type: ignore[misc]
