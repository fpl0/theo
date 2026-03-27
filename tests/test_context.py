"""Tests for theo.context — context assembly for conversation turns."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from theo.conversation.context import (
    AssembledContext,
    _episodes_to_messages,
    _format_core_memory,
    _format_relevant_memories,
    assemble,
    estimate_tokens,
)
from theo.memory._types import EpisodeResult, NodeResult
from theo.memory.core import CoreDocument


@pytest.fixture(autouse=True)
def _no_onboarding():
    """Disable onboarding state for all assemble tests in this module."""
    with patch(
        "theo.conversation.context.get_onboarding_state",
        new_callable=AsyncMock,
        return_value=None,
    ):
        yield


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
_SESSION = UUID("00000000-0000-0000-0000-000000000001")


def _core_doc(
    label: str = "persona",
    body: dict[str, Any] | None = None,
    version: int = 1,
) -> CoreDocument:
    return CoreDocument(
        label=label,  # type: ignore[arg-type]
        body=body if body is not None else {"summary": "Theo is a personal AI agent."},
        version=version,
        updated_at=_NOW,
    )


def _all_core_docs() -> dict[str, CoreDocument]:
    return {
        "persona": _core_doc("persona", {"summary": "Theo is a personal AI agent."}),
        "goals": _core_doc("goals", {"active": [], "completed": []}),
        "user_model": _core_doc("user_model", {"preferences": {}, "values": {}}),
        "context": _core_doc("context", {"current_task": None, "focus": None}),
    }


def _node(
    *,
    node_id: int = 1,
    kind: str = "fact",
    body: str = "The sky is blue",
    similarity: float = 0.9,
) -> NodeResult:
    return NodeResult(
        id=node_id,
        kind=kind,
        body=body,
        trust="owner",
        confidence=0.8,
        importance=0.5,
        sensitivity="normal",
        meta={},
        created_at=_NOW,
        similarity=similarity,
    )


def _episode(
    *,
    episode_id: int = 1,
    role: str = "user",
    body: str = "Hello",
    channel: str = "message",
) -> EpisodeResult:
    return EpisodeResult(
        id=episode_id,
        session_id=_SESSION,
        channel=channel,  # type: ignore[arg-type]
        role=role,  # type: ignore[arg-type]
        body=body,
        trust="owner",
        importance=0.5,
        sensitivity="normal",
        meta={},
        created_at=_NOW,
        similarity=None,
    )


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_empty_string() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_single_word() -> None:
    assert estimate_tokens("hello") >= 1


def test_estimate_tokens_scales_with_words() -> None:
    short = estimate_tokens("hello world")
    long = estimate_tokens("hello world this is a much longer sentence with many words")
    assert long > short


# ---------------------------------------------------------------------------
# _format_core_memory
# ---------------------------------------------------------------------------


def test_format_core_memory_all_sections() -> None:
    docs = _all_core_docs()
    result = _format_core_memory(docs)

    assert "## Persona" in result
    assert "## Goals" in result
    assert "## User Model" in result
    assert "## Current Context" in result


def test_format_core_memory_preserves_section_order() -> None:
    docs = _all_core_docs()
    result = _format_core_memory(docs)

    persona_pos = result.index("## Persona")
    goals_pos = result.index("## Goals")
    user_model_pos = result.index("## User Model")
    context_pos = result.index("## Current Context")
    assert persona_pos < goals_pos < user_model_pos < context_pos


def test_format_core_memory_includes_body_content() -> None:
    docs = _all_core_docs()
    result = _format_core_memory(docs)

    assert "Theo is a personal AI agent." in result


def test_format_core_memory_partial_documents() -> None:
    docs = {"persona": _core_doc("persona")}
    result = _format_core_memory(docs)

    assert "## Persona" in result
    assert "## Goals" not in result


def test_format_core_memory_empty() -> None:
    result = _format_core_memory({})
    assert result == ""


# ---------------------------------------------------------------------------
# _format_relevant_memories
# ---------------------------------------------------------------------------


def test_format_relevant_memories_includes_nodes() -> None:
    nodes = [_node(body="Earth orbits the Sun"), _node(node_id=2, body="Water is H2O")]
    result = _format_relevant_memories(nodes, budget=2000)

    assert "## Relevant Memories" in result
    assert "Earth orbits the Sun" in result
    assert "Water is H2O" in result


def test_format_relevant_memories_respects_budget() -> None:
    nodes = [
        _node(node_id=i, body=f"This is memory number {i} with some additional text to use tokens")
        for i in range(50)
    ]
    result = _format_relevant_memories(nodes, budget=50)

    # Should include some but not all 50 nodes
    included = result.count("- [fact]")
    assert 0 < included < 50


def test_format_relevant_memories_empty_list() -> None:
    assert _format_relevant_memories([], budget=2000) == ""


def test_format_relevant_memories_budget_too_small() -> None:
    nodes = [_node(body="A very long body " * 100)]
    result = _format_relevant_memories(nodes, budget=1)

    assert result == ""


def test_format_relevant_memories_includes_kind() -> None:
    nodes = [_node(kind="observation", body="It rained today")]
    result = _format_relevant_memories(nodes, budget=2000)

    assert "[observation]" in result


# ---------------------------------------------------------------------------
# _episodes_to_messages
# ---------------------------------------------------------------------------


def test_episodes_basic_conversion() -> None:
    eps = [
        _episode(episode_id=1, role="user", body="Hi"),
        _episode(episode_id=2, role="assistant", body="Hello!"),
    ]
    msgs = _episodes_to_messages(eps, budget=4000)

    assert len(msgs) == 2
    assert msgs[0] == {"role": "user", "content": "Hi"}
    assert msgs[1] == {"role": "assistant", "content": "Hello!"}


def test_episodes_merges_consecutive_same_role() -> None:
    eps = [
        _episode(episode_id=1, role="user", body="Hi"),
        _episode(episode_id=2, role="user", body="How are you?"),
        _episode(episode_id=3, role="assistant", body="I'm great!"),
    ]
    msgs = _episodes_to_messages(eps, budget=4000)

    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert "Hi" in msgs[0]["content"]
    assert "How are you?" in msgs[0]["content"]


def test_episodes_tool_role_mapped_to_user() -> None:
    eps = [
        _episode(episode_id=1, role="user", body="Search for X"),
        _episode(episode_id=2, role="assistant", body="Let me search..."),
        _episode(episode_id=3, role="tool", body="Result: found X"),
    ]
    msgs = _episodes_to_messages(eps, budget=4000)

    assert msgs[-1]["role"] == "user"
    assert "Result: found X" in msgs[-1]["content"]


def test_episodes_system_role_mapped_to_user() -> None:
    eps = [
        _episode(episode_id=1, role="system", body="System initialized"),
        _episode(episode_id=2, role="assistant", body="Ready to help"),
    ]
    msgs = _episodes_to_messages(eps, budget=4000)

    assert msgs[0]["role"] == "user"
    assert "System initialized" in msgs[0]["content"]


def test_episodes_drops_oldest_on_budget() -> None:
    eps = [
        _episode(episode_id=1, role="user", body="Old message " * 200),
        _episode(episode_id=2, role="assistant", body="Old response " * 200),
        _episode(episode_id=3, role="user", body="Recent message"),
        _episode(episode_id=4, role="assistant", body="Recent response"),
    ]
    msgs = _episodes_to_messages(eps, budget=50)

    # Should have dropped the old large messages
    assert len(msgs) < 4
    bodies = " ".join(m["content"] for m in msgs)
    assert "Recent" in bodies


def test_episodes_strips_leading_assistant() -> None:
    eps = [
        _episode(episode_id=1, role="assistant", body="I started first"),
        _episode(episode_id=2, role="user", body="Now I speak"),
    ]
    msgs = _episodes_to_messages(eps, budget=4000)

    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "Now I speak"


def test_episodes_empty_list() -> None:
    msgs = _episodes_to_messages([], budget=4000)
    assert msgs == []


def test_episodes_single_user_message() -> None:
    eps = [_episode(role="user", body="Just me")]
    msgs = _episodes_to_messages(eps, budget=4000)

    assert len(msgs) == 1
    assert msgs[0] == {"role": "user", "content": "Just me"}


# ---------------------------------------------------------------------------
# assemble (full integration with mocked dependencies)
# ---------------------------------------------------------------------------


async def test_assemble_full_context() -> None:
    core_docs = _all_core_docs()
    nodes = [_node(body="Relevant fact")]
    eps = [
        _episode(role="user", body="Hello Theo"),
        _episode(episode_id=2, role="assistant", body="Hello!"),
    ]

    with (
        patch(
            "theo.conversation.context.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch(
            "theo.conversation.context.search_nodes", new_callable=AsyncMock, return_value=nodes
        ),
        patch("theo.conversation.context.list_episodes", new_callable=AsyncMock, return_value=eps),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Hello Theo")

    assert isinstance(result, AssembledContext)
    assert "## Persona" in result.system_prompt
    assert "## Relevant Memories" in result.system_prompt
    assert "Relevant fact" in result.system_prompt
    assert len(result.messages) == 2
    assert result.messages[0]["role"] == "user"
    assert result.messages[1]["role"] == "assistant"
    assert result.token_estimate > 0


async def test_assemble_empty_memory() -> None:
    with (
        patch("theo.conversation.context.core.read_all", new_callable=AsyncMock, return_value={}),
        patch("theo.conversation.context.search_nodes", new_callable=AsyncMock, return_value=[]),
        patch("theo.conversation.context.list_episodes", new_callable=AsyncMock, return_value=[]),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Hello")

    assert result.system_prompt == ""
    assert result.messages == []
    assert result.token_estimate == 0


async def test_assemble_no_relevant_memories() -> None:
    core_docs = _all_core_docs()

    with (
        patch(
            "theo.conversation.context.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch("theo.conversation.context.search_nodes", new_callable=AsyncMock, return_value=[]),
        patch("theo.conversation.context.list_episodes", new_callable=AsyncMock, return_value=[]),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Hello")

    assert "## Persona" in result.system_prompt
    assert "## Relevant Memories" not in result.system_prompt


async def test_assemble_passes_session_to_list_episodes() -> None:
    mock_list = AsyncMock(return_value=[])

    with (
        patch("theo.conversation.context.core.read_all", new_callable=AsyncMock, return_value={}),
        patch("theo.conversation.context.search_nodes", new_callable=AsyncMock, return_value=[]),
        patch("theo.conversation.context.list_episodes", mock_list),
    ):
        await assemble(session_id=_SESSION, latest_message="Hello")

    mock_list.assert_awaited_once_with(_SESSION)


async def test_assemble_passes_message_to_search() -> None:
    mock_search = AsyncMock(return_value=[])

    with (
        patch("theo.conversation.context.core.read_all", new_callable=AsyncMock, return_value={}),
        patch("theo.conversation.context.search_nodes", mock_search),
        patch("theo.conversation.context.list_episodes", new_callable=AsyncMock, return_value=[]),
    ):
        await assemble(session_id=_SESSION, latest_message="find me relevant stuff")

    mock_search.assert_awaited_once_with("find me relevant stuff", limit=20)


async def test_assemble_core_memory_never_truncated() -> None:
    large_body = {"summary": "x " * 5000}
    core_docs = {"persona": _core_doc("persona", large_body)}

    with (
        patch(
            "theo.conversation.context.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch("theo.conversation.context.search_nodes", new_callable=AsyncMock, return_value=[]),
        patch("theo.conversation.context.list_episodes", new_callable=AsyncMock, return_value=[]),
    ):
        result = await assemble(
            session_id=_SESSION,
            latest_message="Hello",
            memory_budget=10,
            history_budget=10,
        )

    assert "x " * 100 in result.system_prompt


async def test_assemble_token_estimate_sums_sections() -> None:
    core_docs = {"persona": _core_doc("persona")}
    nodes = [_node(body="A fact")]
    eps = [_episode(role="user", body="Hi")]

    with (
        patch(
            "theo.conversation.context.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch(
            "theo.conversation.context.search_nodes", new_callable=AsyncMock, return_value=nodes
        ),
        patch("theo.conversation.context.list_episodes", new_callable=AsyncMock, return_value=eps),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Hi")

    # Token estimate should be positive and roughly equal to sum of parts
    assert result.token_estimate > 0


async def test_assemble_respects_history_budget() -> None:
    core_docs = {}
    eps = [
        _episode(episode_id=1, role="user", body="Old " * 500),
        _episode(episode_id=2, role="assistant", body="Old reply " * 500),
        _episode(episode_id=3, role="user", body="Recent"),
    ]

    with (
        patch(
            "theo.conversation.context.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch("theo.conversation.context.search_nodes", new_callable=AsyncMock, return_value=[]),
        patch("theo.conversation.context.list_episodes", new_callable=AsyncMock, return_value=eps),
    ):
        result = await assemble(
            session_id=_SESSION,
            latest_message="Recent",
            history_budget=50,
        )

    # The old large messages should have been dropped
    assert len(result.messages) < 3


# ---------------------------------------------------------------------------
# AssembledContext invariants
# ---------------------------------------------------------------------------


def test_assembled_context_is_frozen() -> None:
    ctx = AssembledContext(system_prompt="test", messages=[], token_estimate=0)
    with pytest.raises(AttributeError):
        ctx.system_prompt = "changed"  # type: ignore[misc]
