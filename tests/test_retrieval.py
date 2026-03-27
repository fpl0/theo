"""Tests for theo.memory.retrieval — hybrid search with RRF fusion."""

from __future__ import annotations

import dataclasses
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from theo.config import Settings
from theo.memory import NodeResult
from theo.memory.retrieval import hybrid_search

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
_DIM = 768


def _fake_vector() -> np.ndarray:
    vec = np.random.default_rng(42).standard_normal(_DIM).astype(np.float32)
    return vec / np.linalg.norm(vec)


@dataclasses.dataclass(slots=True)
class _RowSpec:
    node_id: int = 1
    kind: str = "fact"
    body: str = "Python was created by Guido van Rossum"
    trust: str = "inferred"
    confidence: float = 0.8
    importance: float = 0.7
    sensitivity: str = "normal"
    similarity: float = 0.05
    in_vector: bool = True
    in_fts: bool = False
    in_graph: bool = False


def _result_row(**kwargs: object) -> dict:
    spec = _RowSpec(**kwargs)
    return {
        "id": spec.node_id,
        "kind": spec.kind,
        "body": spec.body,
        "trust": spec.trust,
        "confidence": spec.confidence,
        "importance": spec.importance,
        "sensitivity": spec.sensitivity,
        "meta": {},
        "created_at": _NOW,
        "similarity": spec.similarity,
        "in_vector": spec.in_vector,
        "in_fts": spec.in_fts,
        "in_graph": spec.in_graph,
    }


@pytest.fixture
def mock_pool() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_embedder() -> AsyncMock:
    mock = AsyncMock()
    mock.embed_one.return_value = _fake_vector()
    return mock


def _patch(mock_pool: AsyncMock, mock_embedder: AsyncMock) -> AbstractContextManager[None]:
    """Context manager that patches db pool, embedder, and config."""
    cfg = Settings(
        database_url="postgresql://theo:test@localhost/theo",
        anthropic_api_key="sk-ant-test",
        retrieval_rrf_k=60,
        retrieval_candidate_limit=50,
        retrieval_graph_seed_count=5,
        retrieval_graph_max_depth=2,
        _env_file=None,
    )

    @contextmanager
    def _cm() -> Iterator[None]:
        with (
            patch("theo.memory.retrieval.db", pool=mock_pool),
            patch("theo.memory.retrieval.embedder", mock_embedder),
            patch("theo.memory.retrieval.get_settings", return_value=cfg),
        ):
            yield

    return _cm()


# ---------------------------------------------------------------------------
# Core RRF fusion behaviour
# ---------------------------------------------------------------------------


async def test_hybrid_search_returns_node_results(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    mock_pool.fetch.return_value = [
        _result_row(node_id=1, similarity=0.05, in_vector=True, in_fts=True, in_graph=True),
        _result_row(node_id=2, similarity=0.03, in_vector=True, in_fts=False, in_graph=False),
    ]

    with _patch(mock_pool, mock_embedder):
        results = await hybrid_search("test query", limit=5)

    assert len(results) == 2
    assert all(isinstance(r, NodeResult) for r in results)
    mock_embedder.embed_one.assert_awaited_once_with("test query")


async def test_node_in_all_signals_ranks_higher(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    """A node appearing in all three signals gets a higher RRF score."""
    all_three_score = 1.0 / (60 + 1) + 1.0 / (60 + 1) + 1.0 / (60 + 1)
    vector_only_score = 1.0 / (60 + 2)
    mock_pool.fetch.return_value = [
        _result_row(
            node_id=1,
            similarity=all_three_score,
            in_vector=True,
            in_fts=True,
            in_graph=True,
        ),
        _result_row(
            node_id=2,
            similarity=vector_only_score,
            in_vector=True,
            in_fts=False,
            in_graph=False,
        ),
    ]

    with _patch(mock_pool, mock_embedder):
        results = await hybrid_search("multi-signal query")

    assert results[0].id == 1
    assert results[1].id == 2
    assert results[0].similarity is not None
    assert results[1].similarity is not None
    assert results[0].similarity > results[1].similarity


async def test_fts_boost_ranks_keyword_match_higher(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    """An exact FTS keyword match boosts ranking above a vector-only result."""
    vector_plus_fts = 1.0 / (60 + 3) + 1.0 / (60 + 1)
    vector_only = 1.0 / (60 + 1)
    mock_pool.fetch.return_value = [
        _result_row(
            node_id=10,
            body="exact keyword match",
            similarity=vector_plus_fts,
            in_vector=True,
            in_fts=True,
        ),
        _result_row(
            node_id=20,
            body="semantically similar",
            similarity=vector_only,
            in_vector=True,
            in_fts=False,
        ),
    ]

    with _patch(mock_pool, mock_embedder):
        results = await hybrid_search("keyword")

    assert results[0].id == 10
    assert results[0].similarity is not None
    assert results[1].similarity is not None
    assert results[0].similarity > results[1].similarity


async def test_graph_neighbors_appear_in_results(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    """Graph neighbors of top vector hits appear even if not vector-ranked."""
    mock_pool.fetch.return_value = [
        _result_row(node_id=1, similarity=0.016, in_vector=True),
        _result_row(node_id=99, similarity=0.014, in_graph=True, in_vector=False),
    ]

    with _patch(mock_pool, mock_embedder):
        results = await hybrid_search("graph traversal test")

    assert len(results) == 2
    graph_only = [r for r in results if r.id == 99]
    assert len(graph_only) == 1


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


async def test_graceful_when_no_edges_exist(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    """When graph CTE returns empty, vector + FTS still produce results."""
    mock_pool.fetch.return_value = [
        _result_row(node_id=1, in_vector=True, in_fts=True, in_graph=False),
        _result_row(node_id=2, in_vector=True, in_fts=False, in_graph=False),
    ]

    with _patch(mock_pool, mock_embedder):
        results = await hybrid_search("no edges scenario")

    assert len(results) == 2
    assert all(r.similarity is not None for r in results)


async def test_graceful_when_fts_matches_nothing(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    """When FTS produces no matches, vector + graph still work."""
    mock_pool.fetch.return_value = [
        _result_row(node_id=1, in_vector=True, in_fts=False, in_graph=True),
    ]

    with _patch(mock_pool, mock_embedder):
        results = await hybrid_search("xyzzy unknown word")

    assert len(results) == 1


async def test_empty_results(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    mock_pool.fetch.return_value = []

    with _patch(mock_pool, mock_embedder):
        results = await hybrid_search("nothing matches")

    assert results == []


# ---------------------------------------------------------------------------
# Kind filter
# ---------------------------------------------------------------------------


async def test_kind_filter_uses_filtered_query(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    mock_pool.fetch.return_value = [
        _result_row(node_id=5, kind="person", in_vector=True),
    ]

    with _patch(mock_pool, mock_embedder):
        results = await hybrid_search("find people", kind="person", limit=5)

    assert len(results) == 1
    assert results[0].kind == "person"

    # Verify the kind parameter ($8) was passed
    args = mock_pool.fetch.call_args.args
    assert args[8] == "person"


async def test_no_kind_uses_unfiltered_query(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    mock_pool.fetch.return_value = []

    with _patch(mock_pool, mock_embedder):
        await hybrid_search("anything")

    # Unfiltered: 7 positional args (SQL + $1..$7)
    args = mock_pool.fetch.call_args.args
    assert len(args) == 8  # SQL + 7 params


# ---------------------------------------------------------------------------
# Config parameter passing
# ---------------------------------------------------------------------------


async def test_config_parameters_passed_correctly(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    mock_pool.fetch.return_value = []

    with _patch(mock_pool, mock_embedder):
        await hybrid_search("test", limit=15)

    args = mock_pool.fetch.call_args.args
    # $2 = candidate_limit (50), $4 = rrf_k (60),
    # $5 = graph_max_depth (2), $6 = seed_count (5), $7 = limit (15)
    assert args[2] == 50  # retrieval_candidate_limit
    assert args[4] == 60  # retrieval_rrf_k
    assert args[5] == 2  # retrieval_graph_max_depth
    assert args[6] == 5  # retrieval_graph_seed_count
    assert args[7] == 15  # final limit


async def test_default_limit_is_10(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    mock_pool.fetch.return_value = []

    with _patch(mock_pool, mock_embedder):
        await hybrid_search("query")

    args = mock_pool.fetch.call_args.args
    assert args[7] == 10  # default limit


# ---------------------------------------------------------------------------
# RRF score ordering
# ---------------------------------------------------------------------------


async def test_results_ordered_by_rrf_score_descending(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    mock_pool.fetch.return_value = [
        _result_row(node_id=1, similarity=0.049),
        _result_row(node_id=2, similarity=0.032),
        _result_row(node_id=3, similarity=0.016),
    ]

    with _patch(mock_pool, mock_embedder):
        results = await hybrid_search("ordered test")

    similarities = [r.similarity for r in results]
    assert similarities == sorted(similarities, reverse=True)


# ---------------------------------------------------------------------------
# Result type carries RRF score
# ---------------------------------------------------------------------------


async def test_similarity_field_carries_rrf_score(
    mock_pool: AsyncMock,
    mock_embedder: AsyncMock,
) -> None:
    rrf_score = 1.0 / (60 + 1) + 1.0 / (60 + 2)
    mock_pool.fetch.return_value = [
        _result_row(node_id=1, similarity=rrf_score, in_vector=True, in_fts=True),
    ]

    with _patch(mock_pool, mock_embedder):
        results = await hybrid_search("rrf score test")

    assert len(results) == 1
    assert results[0].similarity == pytest.approx(rrf_score)
