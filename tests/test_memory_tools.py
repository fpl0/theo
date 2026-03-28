"""Tests for memory tool definitions and execution."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from theo.memory.tools import TOOL_DEFINITIONS, execute_tool

# -- Fake types for testing ------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _FakeNode:
    id: int = 1
    kind: str = "fact"
    body: str = "Python is great"
    trust: str = "inferred"
    confidence: float = 0.9
    importance: float = 0.7
    sensitivity: str = "normal"
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)
    created_at: datetime = dataclasses.field(default_factory=lambda: datetime.now(UTC))
    similarity: float | None = 0.95


@dataclasses.dataclass(frozen=True)
class _FakeDoc:
    label: str
    body: dict[str, Any]
    version: int
    updated_at: datetime


# -- Tool definition tests -------------------------------------------------


class TestToolDefinitions:
    def test_all_tools_defined(self) -> None:
        names = {t["name"] for t in TOOL_DEFINITIONS}
        expected = {
            "store_memory",
            "search_memory",
            "read_core_memory",
            "update_core_memory",
            "link_memories",
            "update_user_model",
            "advance_onboarding",
        }
        assert names == expected

    def test_each_tool_has_required_fields(self) -> None:
        for tool in TOOL_DEFINITIONS:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["input_schema"]["type"] == "object"

    def test_store_memory_requires_kind_and_body(self) -> None:
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "store_memory")
        assert set(tool["input_schema"]["required"]) == {"kind", "body"}

    def test_search_memory_requires_query(self) -> None:
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "search_memory")
        assert tool["input_schema"]["required"] == ["query"]

    def test_read_core_memory_has_no_required_params(self) -> None:
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "read_core_memory")
        assert "required" not in tool["input_schema"]

    def test_update_core_memory_requires_label_and_body(self) -> None:
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "update_core_memory")
        assert set(tool["input_schema"]["required"]) == {"label", "body"}

    def test_update_core_memory_label_enum(self) -> None:
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "update_core_memory")
        label_prop = tool["input_schema"]["properties"]["label"]
        assert set(label_prop["enum"]) == {
            "persona",
            "goals",
            "user_model",
            "context",
        }


# -- store_memory tests ----------------------------------------------------


class TestStoreMemory:
    async def test_store_memory_calls_store_node(self) -> None:
        mock = AsyncMock(return_value=42)
        with patch("theo.memory.tools.nodes.store_node", mock):
            result = await execute_tool(
                "store_memory",
                {"kind": "fact", "body": "Python is great"},
            )

        parsed = json.loads(result)
        assert parsed == {"stored": True, "node_id": 42}

    async def test_store_memory_passes_importance(self) -> None:
        mock = AsyncMock(return_value=1)
        with patch("theo.memory.tools.nodes.store_node", mock):
            await execute_tool(
                "store_memory",
                {"kind": "preference", "body": "likes coffee", "importance": 0.8},
            )

        mock.assert_awaited_once_with(
            kind="preference",
            body="likes coffee",
            importance=0.8,
        )

    async def test_store_memory_defaults_importance(self) -> None:
        mock = AsyncMock(return_value=1)
        with patch("theo.memory.tools.nodes.store_node", mock):
            await execute_tool("store_memory", {"kind": "fact", "body": "test"})

        _, kwargs = mock.await_args
        assert kwargs["importance"] == 0.5


# -- search_memory tests ---------------------------------------------------


class TestSearchMemory:
    async def test_search_memory_returns_formatted_results(self) -> None:
        mock = AsyncMock(return_value=[_FakeNode()])
        with patch("theo.memory.tools.retrieval.hybrid_search", mock):
            result = await execute_tool(
                "search_memory",
                {"query": "python"},
            )

        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["body"] == "Python is great"
        assert parsed[0]["score"] == 0.95

    async def test_search_memory_passes_limit(self) -> None:
        mock = AsyncMock(return_value=[])
        with patch("theo.memory.tools.retrieval.hybrid_search", mock):
            await execute_tool("search_memory", {"query": "test", "limit": 3})

        mock.assert_awaited_once_with("test", limit=3)

    async def test_search_memory_defaults_limit_to_5(self) -> None:
        mock = AsyncMock(return_value=[])
        with patch("theo.memory.tools.retrieval.hybrid_search", mock):
            await execute_tool("search_memory", {"query": "test"})

        mock.assert_awaited_once_with("test", limit=5)


# -- read_core_memory tests ------------------------------------------------


class TestReadCoreMemory:
    async def test_read_core_memory_returns_all_documents(self) -> None:
        now = datetime.now(UTC)
        fake_docs = {
            "persona": _FakeDoc("persona", {"name": "Theo"}, 1, now),
            "goals": _FakeDoc("goals", {"primary": "help"}, 1, now),
        }
        mock = AsyncMock(return_value=fake_docs)
        with patch("theo.memory.tools.core.read_all", mock):
            result = await execute_tool("read_core_memory", {})

        parsed = json.loads(result)
        assert "persona" in parsed
        assert parsed["persona"]["body"] == {"name": "Theo"}
        assert parsed["persona"]["version"] == 1


# -- update_core_memory tests ----------------------------------------------


class TestUpdateCoreMemory:
    async def test_update_core_memory_calls_update(self) -> None:
        mock = AsyncMock(return_value=2)
        with patch("theo.memory.tools.core.update", mock):
            result = await execute_tool(
                "update_core_memory",
                {
                    "label": "persona",
                    "body": {"name": "Theo v2"},
                    "reason": "evolved",
                },
            )

        parsed = json.loads(result)
        assert parsed == {"updated": True, "label": "persona", "version": 2}
        mock.assert_awaited_once_with(
            "persona",
            body={"name": "Theo v2"},
            reason="evolved",
        )

    async def test_update_core_memory_without_reason(self) -> None:
        mock = AsyncMock(return_value=3)
        with patch("theo.memory.tools.core.update", mock):
            await execute_tool(
                "update_core_memory",
                {"label": "goals", "body": {"primary": "learn"}},
            )

        _, kwargs = mock.await_args
        assert kwargs["reason"] is None

    async def test_update_core_memory_rejects_non_dict_body(self) -> None:
        result = await execute_tool(
            "update_core_memory",
            {"label": "persona", "body": "not a dict"},
        )
        assert "Error" in result


# -- Error handling tests ---------------------------------------------------


class TestErrorHandling:
    async def test_unknown_tool_returns_error_string(self) -> None:
        result = await execute_tool("nonexistent_tool", {})
        assert "Unknown tool" in result

    async def test_tool_exception_returns_error_string(self) -> None:
        with patch(
            "theo.memory.tools.nodes.store_node",
            AsyncMock(side_effect=RuntimeError("db down")),
        ):
            result = await execute_tool(
                "store_memory",
                {"kind": "fact", "body": "test"},
            )

        assert "Error executing store_memory" in result
        assert "db down" in result

    async def test_tool_exception_does_not_raise(self) -> None:
        with patch(
            "theo.memory.tools.retrieval.hybrid_search",
            AsyncMock(side_effect=ConnectionError("timeout")),
        ):
            result = await execute_tool("search_memory", {"query": "test"})

        assert isinstance(result, str)


# -- link_memories tests ---------------------------------------------------


class TestLinkMemories:
    async def test_link_memories_creates_edge(self) -> None:
        mock = AsyncMock(return_value=99)
        with patch("theo.memory.tools.edges.store_edge", mock):
            result = await execute_tool(
                "link_memories",
                {"source_id": 1, "target_id": 2, "label": "works_on"},
            )

        parsed = json.loads(result)
        assert parsed == {"linked": True, "edge_id": 99}
        mock.assert_awaited_once_with(
            source_id=1,
            target_id=2,
            label="works_on",
            weight=0.8,
            meta={"source": "llm_tool"},
        )

    async def test_link_memories_includes_reason_in_meta(self) -> None:
        mock = AsyncMock(return_value=100)
        with patch("theo.memory.tools.edges.store_edge", mock):
            await execute_tool(
                "link_memories",
                {
                    "source_id": 1,
                    "target_id": 2,
                    "label": "related_to",
                    "reason": "both about Python",
                },
            )

        _, kwargs = mock.await_args
        assert kwargs["meta"]["reason"] == "both about Python"

    async def test_link_memories_rejects_non_int_ids(self) -> None:
        result = await execute_tool(
            "link_memories",
            {"source_id": "abc", "target_id": 2, "label": "related_to"},
        )
        assert "Error" in result

    async def test_link_memories_rejects_non_positive_ids(self) -> None:
        result = await execute_tool(
            "link_memories",
            {"source_id": 0, "target_id": 2, "label": "related_to"},
        )
        assert "Error" in result


# -- update_user_model tests -----------------------------------------------


@dataclasses.dataclass(frozen=True)
class _FakeDimensionResult:
    framework: str = "big_five"
    dimension: str = "openness"
    confidence: float = 0.7
    evidence_count: int = 3


class TestUpdateUserModel:
    async def test_update_user_model_calls_update_dimension(self) -> None:
        mock = AsyncMock(return_value=_FakeDimensionResult())
        with patch("theo.memory.tools.user_model.update_dimension", mock):
            result = await execute_tool(
                "update_user_model",
                {
                    "framework": "big_five",
                    "dimension": "openness",
                    "value": {"score": 0.8, "label": "high"},
                    "reason": "user described creative interests",
                },
            )

        parsed = json.loads(result)
        assert parsed["updated"] is True
        assert parsed["framework"] == "big_five"
        assert parsed["dimension"] == "openness"
        mock.assert_awaited_once_with(
            "big_five",
            "openness",
            value={"score": 0.8, "label": "high"},
            reason="user described creative interests",
        )

    async def test_update_user_model_rejects_non_dict_value(self) -> None:
        result = await execute_tool(
            "update_user_model",
            {"framework": "big_five", "dimension": "openness", "value": "not a dict"},
        )
        assert "Error" in result

    async def test_update_user_model_without_reason(self) -> None:
        mock = AsyncMock(return_value=_FakeDimensionResult())
        with patch("theo.memory.tools.user_model.update_dimension", mock):
            await execute_tool(
                "update_user_model",
                {
                    "framework": "big_five",
                    "dimension": "openness",
                    "value": {"score": 0.8},
                },
            )

        _, kwargs = mock.await_args
        assert kwargs["reason"] is None


# -- store_memory with episode_id tests ------------------------------------


class TestStoreMemoryEpisodeId:
    async def test_store_memory_records_mention_when_episode_id_provided(self) -> None:
        mock_store = AsyncMock(return_value=42)
        mock_mention = AsyncMock()
        with (
            patch("theo.memory.tools.nodes.store_node", mock_store),
            patch("theo.memory.tools.record_mention", mock_mention),
        ):
            result = await execute_tool(
                "store_memory",
                {"kind": "fact", "body": "test"},
                episode_id=10,
            )

        parsed = json.loads(result)
        assert parsed["node_id"] == 42
        mock_mention.assert_awaited_once_with(10, 42)

    async def test_store_memory_skips_mention_when_no_episode_id(self) -> None:
        mock_store = AsyncMock(return_value=42)
        mock_mention = AsyncMock()
        with (
            patch("theo.memory.tools.nodes.store_node", mock_store),
            patch("theo.memory.tools.record_mention", mock_mention),
        ):
            await execute_tool(
                "store_memory",
                {"kind": "fact", "body": "test"},
            )

        mock_mention.assert_not_awaited()


# -- advance_onboarding tests -----------------------------------------------


class TestAdvanceOnboarding:
    async def test_advance_onboarding_no_active_session(self) -> None:
        mock_get = AsyncMock(return_value=None)
        with patch("theo.memory.tools.onboarding_flow.get_onboarding_state", mock_get):
            result = await execute_tool("advance_onboarding", {"summary": "done"})

        parsed = json.loads(result)
        assert "error" in parsed

    async def test_advance_onboarding_completes(self) -> None:
        mock_state = MagicMock(phase="intro", phase_index=0)
        mock_get = AsyncMock(return_value=mock_state)
        mock_advance = AsyncMock(return_value=None)

        with (
            patch("theo.memory.tools.onboarding_flow.get_onboarding_state", mock_get),
            patch("theo.memory.tools.onboarding_flow.advance_phase", mock_advance),
        ):
            result = await execute_tool("advance_onboarding", {"summary": "all done"})

        parsed = json.loads(result)
        assert parsed["completed"] is True

    async def test_advance_onboarding_moves_to_next_phase(self) -> None:
        mock_state = MagicMock(phase="intro", phase_index=0)
        mock_next = MagicMock(phase="values", phase_index=1)
        mock_get = AsyncMock(return_value=mock_state)
        mock_advance = AsyncMock(return_value=mock_next)

        with (
            patch("theo.memory.tools.onboarding_flow.get_onboarding_state", mock_get),
            patch("theo.memory.tools.onboarding_flow.advance_phase", mock_advance),
        ):
            result = await execute_tool("advance_onboarding", {"summary": "learned intro"})

        parsed = json.loads(result)
        assert parsed["phase"] == "values"
        assert parsed["phase_index"] == 1
