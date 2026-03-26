"""Tests for theo.memory.episodes — store, list, search operations."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import numpy as np
import pytest

from theo.memory import EpisodeResult
from theo.memory.episodes import list_episodes, search_episodes, store_episode

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 1, 15, 12, 5, 0, tzinfo=UTC)
_DIM = 768
_SESSION = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
_SESSION_2 = UUID("11111111-2222-3333-4444-555555555555")


def _fake_vector() -> np.ndarray:
    vec = np.random.default_rng(42).standard_normal(_DIM).astype(np.float32)
    return vec / np.linalg.norm(vec)


def _episode_row(  # noqa: PLR0913
    *,
    episode_id: int = 1,
    session_id: UUID = _SESSION,
    channel: str = "message",
    role: str = "user",
    body: str = "Hello, Theo",
    trust: str = "owner",
    importance: float = 0.5,
    sensitivity: str = "normal",
    created_at: datetime = _NOW,
    similarity: float | None = None,
) -> dict:
    row: dict = {
        "id": episode_id,
        "session_id": session_id,
        "channel": channel,
        "role": role,
        "body": body,
        "trust": trust,
        "importance": importance,
        "sensitivity": sensitivity,
        "meta": {},
        "created_at": created_at,
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
# store_episode
# ---------------------------------------------------------------------------


async def test_store_inserts_and_returns_id(
    mock_pool: AsyncMock, mock_embedder: AsyncMock
) -> None:
    mock_pool.fetchval.return_value = 1

    with (
        patch("theo.memory.episodes.db", pool=mock_pool),
        patch("theo.memory.episodes.embedder", mock_embedder),
    ):
        result = await store_episode(session_id=_SESSION, role="user", body="Hello, Theo")

    assert result == 1
    mock_embedder.embed_one.assert_awaited_once_with("Hello, Theo")
    mock_pool.fetchval.assert_awaited_once()


async def test_store_passes_custom_fields(mock_pool: AsyncMock, mock_embedder: AsyncMock) -> None:
    mock_pool.fetchval.return_value = 42

    with (
        patch("theo.memory.episodes.db", pool=mock_pool),
        patch("theo.memory.episodes.embedder", mock_embedder),
    ):
        result = await store_episode(
            session_id=_SESSION,
            channel="web",
            role="assistant",
            body="I understand",
            trust="verified",
            importance=0.9,
            sensitivity="sensitive",
            meta={"tool": "browser"},
        )

    assert result == 42
    args = mock_pool.fetchval.call_args.args
    assert args[1] == _SESSION  # session_id
    assert args[2] == "web"  # channel
    assert args[3] == "assistant"  # role
    assert args[4] == "I understand"  # body
    # args[5] is embedding vector
    assert args[6] == "verified"  # trust
    assert args[7] == 0.9  # importance
    assert args[8] == "sensitive"  # sensitivity
    assert args[9] == {"tool": "browser"}  # meta


async def test_store_defaults(mock_pool: AsyncMock, mock_embedder: AsyncMock) -> None:
    mock_pool.fetchval.return_value = 10

    with (
        patch("theo.memory.episodes.db", pool=mock_pool),
        patch("theo.memory.episodes.embedder", mock_embedder),
    ):
        await store_episode(session_id=_SESSION, role="system", body="init")

    args = mock_pool.fetchval.call_args.args
    assert args[2] == "internal"  # default channel
    assert args[6] == "owner"  # default trust
    assert args[7] == 0.5  # default importance
    assert args[8] == "normal"  # default sensitivity
    assert args[9] == {}  # default meta


# ---------------------------------------------------------------------------
# list_episodes
# ---------------------------------------------------------------------------


async def test_list_returns_chronological_episodes(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = [
        _episode_row(episode_id=1, body="first", created_at=_NOW),
        _episode_row(episode_id=2, body="second", created_at=_LATER),
    ]

    with patch("theo.memory.episodes.db", pool=mock_pool):
        results = await list_episodes(_SESSION)

    assert len(results) == 2
    assert all(isinstance(r, EpisodeResult) for r in results)
    assert results[0].id == 1
    assert results[0].body == "first"
    assert results[1].id == 2
    assert results[1].body == "second"
    assert results[0].created_at < results[1].created_at


async def test_list_passes_session_and_limit(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = []

    with patch("theo.memory.episodes.db", pool=mock_pool):
        await list_episodes(_SESSION, limit=25)

    args = mock_pool.fetch.call_args.args
    assert args[1] == _SESSION
    assert args[2] == 25


async def test_list_default_limit(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = []

    with patch("theo.memory.episodes.db", pool=mock_pool):
        await list_episodes(_SESSION)

    args = mock_pool.fetch.call_args.args
    assert args[2] == 50  # default limit


async def test_list_returns_none_similarity(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = [_episode_row()]

    with patch("theo.memory.episodes.db", pool=mock_pool):
        results = await list_episodes(_SESSION)

    assert results[0].similarity is None


# ---------------------------------------------------------------------------
# search_episodes
# ---------------------------------------------------------------------------


async def test_search_returns_ordered_results(
    mock_pool: AsyncMock, mock_embedder: AsyncMock
) -> None:
    mock_pool.fetch.return_value = [
        _episode_row(episode_id=1, body="closest", similarity=0.95),
        _episode_row(episode_id=2, body="second", similarity=0.80),
        _episode_row(episode_id=3, body="distant", similarity=0.60),
    ]

    with (
        patch("theo.memory.episodes.db", pool=mock_pool),
        patch("theo.memory.episodes.embedder", mock_embedder),
    ):
        results = await search_episodes("test query", limit=3)

    assert len(results) == 3
    assert all(isinstance(r, EpisodeResult) for r in results)
    assert results[0].similarity == 0.95
    assert results[1].similarity == 0.80
    assert results[2].similarity == 0.60
    mock_embedder.embed_one.assert_awaited_once_with("test query")


async def test_search_with_session_filter(mock_pool: AsyncMock, mock_embedder: AsyncMock) -> None:
    mock_pool.fetch.return_value = [
        _episode_row(episode_id=10, session_id=_SESSION, similarity=0.9),
    ]

    with (
        patch("theo.memory.episodes.db", pool=mock_pool),
        patch("theo.memory.episodes.embedder", mock_embedder),
    ):
        results = await search_episodes("find session", session_id=_SESSION, limit=5)

    assert len(results) == 1
    assert results[0].session_id == _SESSION

    # Session-filtered query: vec, session_id, limit
    args = mock_pool.fetch.call_args.args
    assert args[2] == _SESSION
    assert args[3] == 5


async def test_search_without_session_uses_unfiltered_query(
    mock_pool: AsyncMock, mock_embedder: AsyncMock
) -> None:
    mock_pool.fetch.return_value = []

    with (
        patch("theo.memory.episodes.db", pool=mock_pool),
        patch("theo.memory.episodes.embedder", mock_embedder),
    ):
        results = await search_episodes("anything", limit=10)

    assert results == []
    # Unfiltered query: SQL + vec + limit
    args = mock_pool.fetch.call_args.args
    assert len(args) == 3


async def test_search_default_limit(mock_pool: AsyncMock, mock_embedder: AsyncMock) -> None:
    mock_pool.fetch.return_value = []

    with (
        patch("theo.memory.episodes.db", pool=mock_pool),
        patch("theo.memory.episodes.embedder", mock_embedder),
    ):
        await search_episodes("query")

    args = mock_pool.fetch.call_args.args
    assert args[2] == 10  # default limit


# ---------------------------------------------------------------------------
# Result type invariants
# ---------------------------------------------------------------------------


def test_episode_result_is_frozen() -> None:
    result = EpisodeResult(
        id=1,
        session_id=_SESSION,
        channel="message",
        role="user",
        body="test",
        trust="owner",
        importance=0.5,
        sensitivity="normal",
        meta={},
        created_at=_NOW,
    )
    with pytest.raises(AttributeError):
        result.body = "changed"  # type: ignore[misc]


def test_episode_result_fields() -> None:
    result = EpisodeResult(
        id=7,
        session_id=_SESSION,
        channel="web",
        role="assistant",
        body="response text",
        trust="verified",
        importance=0.8,
        sensitivity="sensitive",
        meta={"key": "val"},
        created_at=_NOW,
        similarity=0.92,
    )
    assert result.id == 7
    assert result.session_id == _SESSION
    assert result.channel == "web"
    assert result.role == "assistant"
    assert result.body == "response text"
    assert result.trust == "verified"
    assert result.importance == 0.8
    assert result.sensitivity == "sensitive"
    assert result.meta == {"key": "val"}
    assert result.created_at == _NOW
    assert result.similarity == 0.92
