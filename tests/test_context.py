"""Tests for theo.conversation.context — context assembly for conversation turns."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from theo.config import Settings
from theo.conversation.context import (
    AssembledContext,
    SectionTokens,
    assemble,
    episodes_to_messages,
    estimate_tokens,
    format_relevant_memories,
    truncate_section,
)
from theo.memory._types import EpisodeChannel, EpisodeResult, EpisodeRole, NodeResult
from theo.memory.core import CoreDocument, CoreMemoryLabel

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
_SESSION = UUID("00000000-0000-0000-0000-000000000001")


def _core_doc(
    label: CoreMemoryLabel = "persona",
    body: dict[str, Any] | None = None,
    version: int = 1,
) -> CoreDocument:
    return CoreDocument(
        label=label,
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
    role: EpisodeRole = "user",
    body: str = "Hello",
    channel: EpisodeChannel = "message",
) -> EpisodeResult:
    return EpisodeResult(
        id=episode_id,
        session_id=_SESSION,
        channel=channel,
        role=role,
        body=body,
        trust="owner",
        importance=0.5,
        sensitivity="normal",
        meta={},
        created_at=_NOW,
        similarity=None,
    )


def _settings(**overrides: Any) -> Settings:
    """Create a real Settings instance with test defaults."""
    defaults: dict[str, Any] = {
        "database_url": "postgresql://x:x@localhost/x",
        "anthropic_api_key": "sk-test",
        "_env_file": None,
    }
    return Settings(**(defaults | overrides))


@pytest.fixture(autouse=True)
def _mock_deliver_pending():
    """Stub out deliberation delivery — context tests don't need it."""
    with patch(
        "theo.conversation.context.assembly.deliver_pending",
        new_callable=AsyncMock,
        return_value=[],
    ):
        yield


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
# _truncate_section
# ---------------------------------------------------------------------------


def test_truncate_section_within_budget() -> None:
    text = "Short text here"
    assert truncate_section(text, budget=500) == text


def test_truncate_section_exceeding_budget() -> None:
    text = "word " * 200
    result = truncate_section(text, budget=10)
    assert result != ""
    assert estimate_tokens(result) <= 10


def test_truncate_section_empty_text() -> None:
    assert truncate_section("", budget=500) == ""


def test_truncate_section_zero_budget() -> None:
    assert truncate_section("Some text", budget=0) == ""


# ---------------------------------------------------------------------------
# _format_relevant_memories
# ---------------------------------------------------------------------------


def test_format_relevant_memories_includes_nodes() -> None:
    nodes = [_node(body="Earth orbits the Sun"), _node(node_id=2, body="Water is H2O")]
    result = format_relevant_memories(nodes, budget=2000)

    assert "## Relevant Memories" in result
    assert "Earth orbits the Sun" in result
    assert "Water is H2O" in result


def test_format_relevant_memories_respects_budget() -> None:
    nodes = [
        _node(node_id=i, body=f"This is memory number {i} with some additional text to use tokens")
        for i in range(50)
    ]
    result = format_relevant_memories(nodes, budget=50)

    # Should include some but not all 50 nodes
    included = result.count("- [fact]")
    assert 0 < included < 50


def test_format_relevant_memories_empty_list() -> None:
    assert format_relevant_memories([], budget=2000) == ""


def test_format_relevant_memories_budget_too_small() -> None:
    nodes = [_node(body="A very long body " * 100)]
    result = format_relevant_memories(nodes, budget=1)

    assert result == ""


def test_format_relevant_memories_includes_kind() -> None:
    nodes = [_node(kind="observation", body="It rained today")]
    result = format_relevant_memories(nodes, budget=2000)

    assert "[observation]" in result


# ---------------------------------------------------------------------------
# _episodes_to_messages
# ---------------------------------------------------------------------------


def test_episodes_basic_conversion() -> None:
    eps = [
        _episode(episode_id=1, role="user", body="Hi"),
        _episode(episode_id=2, role="assistant", body="Hello!"),
    ]
    msgs = episodes_to_messages(eps, budget=4000)

    assert len(msgs) == 2
    assert msgs[0] == {"role": "user", "content": "Hi"}
    assert msgs[1] == {"role": "assistant", "content": "Hello!"}


def test_episodes_merges_consecutive_same_role() -> None:
    eps = [
        _episode(episode_id=1, role="user", body="Hi"),
        _episode(episode_id=2, role="user", body="How are you?"),
        _episode(episode_id=3, role="assistant", body="I'm great!"),
    ]
    msgs = episodes_to_messages(eps, budget=4000)

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
    msgs = episodes_to_messages(eps, budget=4000)

    assert msgs[-1]["role"] == "user"
    assert "Result: found X" in msgs[-1]["content"]


def test_episodes_system_role_mapped_to_user() -> None:
    eps = [
        _episode(episode_id=1, role="system", body="System initialized"),
        _episode(episode_id=2, role="assistant", body="Ready to help"),
    ]
    msgs = episodes_to_messages(eps, budget=4000)

    assert msgs[0]["role"] == "user"
    assert "System initialized" in msgs[0]["content"]


def test_episodes_drops_oldest_on_budget() -> None:
    eps = [
        _episode(episode_id=1, role="user", body="Old message " * 200),
        _episode(episode_id=2, role="assistant", body="Old response " * 200),
        _episode(episode_id=3, role="user", body="Recent message"),
        _episode(episode_id=4, role="assistant", body="Recent response"),
    ]
    msgs = episodes_to_messages(eps, budget=50)

    # Should have dropped the old large messages
    assert len(msgs) < 4
    bodies = " ".join(m["content"] for m in msgs)
    assert "Recent" in bodies


def test_episodes_strips_leading_assistant() -> None:
    eps = [
        _episode(episode_id=1, role="assistant", body="I started first"),
        _episode(episode_id=2, role="user", body="Now I speak"),
    ]
    msgs = episodes_to_messages(eps, budget=4000)

    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "Now I speak"


def test_episodes_empty_list() -> None:
    msgs = episodes_to_messages([], budget=4000)
    assert msgs == []


def test_episodes_single_user_message() -> None:
    eps = [_episode(role="user", body="Just me")]
    msgs = episodes_to_messages(eps, budget=4000)

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
            "theo.conversation.context.assembly.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch(
            "theo.conversation.context.assembly.hybrid_search",
            new_callable=AsyncMock,
            return_value=nodes,
        ),
        patch(
            "theo.conversation.context.assembly.list_episodes",
            new_callable=AsyncMock,
            return_value=eps,
        ),
        patch("theo.conversation.context.assembly.get_settings", return_value=_settings()),
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
    assert isinstance(result.section_tokens, SectionTokens)


async def test_assemble_empty_memory() -> None:
    with (
        patch(
            "theo.conversation.context.assembly.core.read_all",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "theo.conversation.context.assembly.hybrid_search",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "theo.conversation.context.assembly.list_episodes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("theo.conversation.context.assembly.get_settings", return_value=_settings()),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Hello")

    assert result.system_prompt == ""
    assert result.messages == []
    assert result.token_estimate == 0


async def test_assemble_no_relevant_memories() -> None:
    core_docs = _all_core_docs()

    with (
        patch(
            "theo.conversation.context.assembly.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch(
            "theo.conversation.context.assembly.hybrid_search",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "theo.conversation.context.assembly.list_episodes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("theo.conversation.context.assembly.get_settings", return_value=_settings()),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Hello")

    assert "## Persona" in result.system_prompt
    assert "## Relevant Memories" not in result.system_prompt


async def test_assemble_passes_session_to_list_episodes() -> None:
    mock_list = AsyncMock(return_value=[])

    with (
        patch(
            "theo.conversation.context.assembly.core.read_all",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "theo.conversation.context.assembly.hybrid_search",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("theo.conversation.context.assembly.list_episodes", mock_list),
        patch("theo.conversation.context.assembly.get_settings", return_value=_settings()),
    ):
        await assemble(session_id=_SESSION, latest_message="Hello")

    mock_list.assert_awaited_once_with(_SESSION)


async def test_assemble_passes_message_to_hybrid_search() -> None:
    mock_search = AsyncMock(return_value=[])

    with (
        patch(
            "theo.conversation.context.assembly.core.read_all",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("theo.conversation.context.assembly.hybrid_search", mock_search),
        patch(
            "theo.conversation.context.assembly.list_episodes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("theo.conversation.context.assembly.get_settings", return_value=_settings()),
    ):
        await assemble(session_id=_SESSION, latest_message="find me relevant stuff")

    mock_search.assert_awaited_once_with("find me relevant stuff", limit=20)


async def test_assemble_persona_never_truncated() -> None:
    """Persona is protected even under extreme budget pressure."""
    large_body = {"summary": "x " * 5000}
    core_docs = {"persona": _core_doc("persona", large_body)}

    with (
        patch(
            "theo.conversation.context.assembly.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch(
            "theo.conversation.context.assembly.hybrid_search",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "theo.conversation.context.assembly.list_episodes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "theo.conversation.context.assembly.get_settings",
            return_value=_settings(context_memory_budget=10, context_history_budget=10),
        ),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Hello")

    # Full persona content must be present -- never truncated
    assert "x " * 100 in result.system_prompt


async def test_assemble_goals_never_truncated() -> None:
    """Goals are protected even under extreme budget pressure."""
    large_goals = {"active": ["goal " * 1000]}
    core_docs = {"goals": _core_doc("goals", large_goals)}

    with (
        patch(
            "theo.conversation.context.assembly.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch(
            "theo.conversation.context.assembly.hybrid_search",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "theo.conversation.context.assembly.list_episodes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "theo.conversation.context.assembly.get_settings",
            return_value=_settings(context_memory_budget=10, context_history_budget=10),
        ),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Hello")

    assert "goal " * 100 in result.system_prompt


async def test_assemble_token_estimate_sums_sections() -> None:
    core_docs = {"persona": _core_doc("persona")}
    nodes = [_node(body="A fact")]
    eps = [_episode(role="user", body="Hi")]

    with (
        patch(
            "theo.conversation.context.assembly.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch(
            "theo.conversation.context.assembly.hybrid_search",
            new_callable=AsyncMock,
            return_value=nodes,
        ),
        patch(
            "theo.conversation.context.assembly.list_episodes",
            new_callable=AsyncMock,
            return_value=eps,
        ),
        patch("theo.conversation.context.assembly.get_settings", return_value=_settings()),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Hi")

    # Token estimate should be positive and equal to the sum of all sections
    assert result.token_estimate > 0
    st = result.section_tokens
    expected = st.persona + st.goals + st.user_model + st.current_task + st.memory + st.history
    assert result.token_estimate == expected


async def test_assemble_respects_history_budget() -> None:
    core_docs: dict[str, CoreDocument] = {}
    eps = [
        _episode(episode_id=1, role="user", body="Old " * 500),
        _episode(episode_id=2, role="assistant", body="Old reply " * 500),
        _episode(episode_id=3, role="user", body="Recent"),
    ]

    with (
        patch(
            "theo.conversation.context.assembly.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch(
            "theo.conversation.context.assembly.hybrid_search",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "theo.conversation.context.assembly.list_episodes",
            new_callable=AsyncMock,
            return_value=eps,
        ),
        patch(
            "theo.conversation.context.assembly.get_settings",
            return_value=_settings(context_history_budget=50),
        ),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Recent")

    # The old large messages should have been dropped
    assert len(result.messages) < 3


async def test_assemble_section_ordering() -> None:
    """System prompt sections appear in the canonical order."""
    core_docs = _all_core_docs()
    nodes = [_node(body="A retrieved memory")]

    with (
        patch(
            "theo.conversation.context.assembly.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch(
            "theo.conversation.context.assembly.hybrid_search",
            new_callable=AsyncMock,
            return_value=nodes,
        ),
        patch(
            "theo.conversation.context.assembly.list_episodes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("theo.conversation.context.assembly.get_settings", return_value=_settings()),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Hello")

    prompt = result.system_prompt
    persona_pos = prompt.index("## Persona")
    goals_pos = prompt.index("## Goals")
    user_model_pos = prompt.index("## User Model")
    context_pos = prompt.index("## Current Context")
    memory_pos = prompt.index("## Relevant Memories")
    assert persona_pos < goals_pos < user_model_pos < context_pos < memory_pos


async def test_assemble_memories_trimmed_before_history() -> None:
    """When budget is tight, memories are trimmed before history."""
    core_docs: dict[str, CoreDocument] = {}
    many_nodes = [_node(node_id=i, body=f"Memory {i} " * 20) for i in range(50)]
    eps = [
        _episode(episode_id=1, role="user", body="Recent user message"),
        _episode(episode_id=2, role="assistant", body="Recent assistant reply"),
    ]

    with (
        patch(
            "theo.conversation.context.assembly.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch(
            "theo.conversation.context.assembly.hybrid_search",
            new_callable=AsyncMock,
            return_value=many_nodes,
        ),
        patch(
            "theo.conversation.context.assembly.list_episodes",
            new_callable=AsyncMock,
            return_value=eps,
        ),
        patch(
            "theo.conversation.context.assembly.get_settings",
            return_value=_settings(context_memory_budget=50),
        ),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Hello")

    # History should be preserved (small messages fit in budget)
    assert len(result.messages) == 2
    # Memories should be trimmed (50 nodes won't fit in budget=50 tokens)
    assert result.section_tokens.memory < estimate_tokens(
        "".join(f"Memory {i} " * 20 for i in range(50))
    )


async def test_assemble_user_model_trimmed_when_exceeds_budget() -> None:
    """User model is capped at context_user_model_budget when it exceeds it."""
    large_user_model = {"preferences": {"pref": "x " * 2000}}
    core_docs = {
        "persona": _core_doc("persona"),
        "user_model": _core_doc("user_model", large_user_model),
    }

    with (
        patch(
            "theo.conversation.context.assembly.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch(
            "theo.conversation.context.assembly.hybrid_search",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "theo.conversation.context.assembly.list_episodes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "theo.conversation.context.assembly.get_settings",
            return_value=_settings(context_user_model_budget=50),
        ),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Hello")

    # User model should be trimmed to ~50 tokens
    assert result.section_tokens.user_model <= 50
    assert result.section_tokens.user_model > 0
    # Persona should be untouched
    assert result.section_tokens.persona > 0


async def test_assemble_task_trimmed_when_exceeds_budget() -> None:
    """Current task is capped at context_current_task_budget when it exceeds it."""
    large_task = {"current_task": "task " * 2000, "focus": None}
    core_docs = {"context": _core_doc("context", large_task)}

    with (
        patch(
            "theo.conversation.context.assembly.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch(
            "theo.conversation.context.assembly.hybrid_search",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "theo.conversation.context.assembly.list_episodes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "theo.conversation.context.assembly.get_settings",
            return_value=_settings(context_current_task_budget=50),
        ),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Hello")

    assert result.section_tokens.current_task <= 50
    assert result.section_tokens.current_task > 0


async def test_assemble_section_tokens_populated() -> None:
    """AssembledContext.section_tokens has correct per-section counts."""
    core_docs = _all_core_docs()

    with (
        patch(
            "theo.conversation.context.assembly.core.read_all",
            new_callable=AsyncMock,
            return_value=core_docs,
        ),
        patch(
            "theo.conversation.context.assembly.hybrid_search",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "theo.conversation.context.assembly.list_episodes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("theo.conversation.context.assembly.get_settings", return_value=_settings()),
    ):
        result = await assemble(session_id=_SESSION, latest_message="Hello")

    st = result.section_tokens
    assert st.persona > 0
    assert st.goals > 0
    assert st.user_model > 0
    assert st.current_task > 0
    assert st.memory == 0  # no nodes returned
    assert st.history == 0  # no episodes returned


# ---------------------------------------------------------------------------
# AssembledContext invariants
# ---------------------------------------------------------------------------


def test_assembled_context_is_frozen() -> None:
    ctx = AssembledContext(
        system_prompt="test",
        messages=[],
        token_estimate=0,
        section_tokens=SectionTokens(
            persona=0,
            goals=0,
            user_model=0,
            current_task=0,
            memory=0,
            history=0,
        ),
    )
    with pytest.raises(AttributeError):
        ctx.system_prompt = "changed"  # type: ignore[misc]


def test_section_tokens_is_frozen() -> None:
    st = SectionTokens(persona=10, goals=10, user_model=5, current_task=5, memory=0, history=0)
    with pytest.raises(AttributeError):
        st.persona = 99  # type: ignore[misc]
