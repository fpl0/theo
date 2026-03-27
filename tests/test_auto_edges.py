"""Tests for theo.memory.auto_edges — record_mention and extract_and_link."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, call, patch
from uuid import uuid4

import pytest

from theo.memory.auto_edges import extract_and_link, record_mention
from theo.memory.tools import TOOL_DEFINITIONS, execute_tool

# ---------------------------------------------------------------------------
# record_mention
# ---------------------------------------------------------------------------


class TestRecordMention:
    async def test_inserts_into_episode_node(self) -> None:
        mock_pool = AsyncMock()
        with patch("theo.memory.auto_edges.db", pool=mock_pool):
            await record_mention(episode_id=10, node_id=20)

        mock_pool.execute.assert_awaited_once()
        args = mock_pool.execute.call_args.args
        assert args[1] == 10  # episode_id
        assert args[2] == 20  # node_id
        assert args[3] == "mention"  # default role

    async def test_custom_role(self) -> None:
        mock_pool = AsyncMock()
        with patch("theo.memory.auto_edges.db", pool=mock_pool):
            await record_mention(episode_id=10, node_id=20, role="subject")

        args = mock_pool.execute.call_args.args
        assert args[3] == "subject"

    async def test_on_conflict_does_nothing(self) -> None:
        """SQL should use ON CONFLICT DO NOTHING for idempotency."""
        mock_pool = AsyncMock()
        with patch("theo.memory.auto_edges.db", pool=mock_pool):
            await record_mention(episode_id=10, node_id=20)

        sql = mock_pool.execute.call_args.args[0]
        assert "ON CONFLICT DO NOTHING" in sql


# ---------------------------------------------------------------------------
# extract_and_link
# ---------------------------------------------------------------------------


class TestExtractAndLink:
    async def test_creates_edges_for_co_occurring_nodes(self) -> None:
        session_id = uuid4()
        mock_pool = AsyncMock()
        mock_pool.fetch.return_value = [
            {"node_a": 1, "node_b": 2, "co_count": 3},
            {"node_a": 1, "node_b": 3, "co_count": 1},
        ]

        mock_store_edge = AsyncMock(return_value=42)

        with (
            patch("theo.memory.auto_edges.db", pool=mock_pool),
            patch("theo.memory.auto_edges.store_edge", mock_store_edge),
        ):
            count = await extract_and_link(session_id)

        assert count == 2
        assert mock_store_edge.await_count == 2

    async def test_weight_scales_with_co_occurrence_count(self) -> None:
        session_id = uuid4()
        mock_pool = AsyncMock()
        mock_pool.fetch.return_value = [
            {"node_a": 1, "node_b": 2, "co_count": 3},  # weight = min(1.0, 0.6) = 0.6
        ]

        mock_store_edge = AsyncMock(return_value=1)

        with (
            patch("theo.memory.auto_edges.db", pool=mock_pool),
            patch("theo.memory.auto_edges.store_edge", mock_store_edge),
        ):
            await extract_and_link(session_id)

        _, kwargs = mock_store_edge.call_args
        assert kwargs["weight"] == pytest.approx(0.6)

    async def test_weight_capped_at_one(self) -> None:
        session_id = uuid4()
        mock_pool = AsyncMock()
        mock_pool.fetch.return_value = [
            {"node_a": 1, "node_b": 2, "co_count": 10},  # weight = min(1.0, 2.0) = 1.0
        ]

        mock_store_edge = AsyncMock(return_value=1)

        with (
            patch("theo.memory.auto_edges.db", pool=mock_pool),
            patch("theo.memory.auto_edges.store_edge", mock_store_edge),
        ):
            await extract_and_link(session_id)

        _, kwargs = mock_store_edge.call_args
        assert kwargs["weight"] == 1.0

    async def test_edge_label_and_meta(self) -> None:
        session_id = uuid4()
        mock_pool = AsyncMock()
        mock_pool.fetch.return_value = [
            {"node_a": 5, "node_b": 7, "co_count": 2},
        ]

        mock_store_edge = AsyncMock(return_value=1)

        with (
            patch("theo.memory.auto_edges.db", pool=mock_pool),
            patch("theo.memory.auto_edges.store_edge", mock_store_edge),
        ):
            await extract_and_link(session_id)

        _, kwargs = mock_store_edge.call_args
        assert kwargs["label"] == "co_occurs"
        assert kwargs["meta"] == {"co_count": 2, "source": "auto_edge"}

    async def test_returns_zero_when_no_co_occurrences(self) -> None:
        session_id = uuid4()
        mock_pool = AsyncMock()
        mock_pool.fetch.return_value = []

        with patch("theo.memory.auto_edges.db", pool=mock_pool):
            count = await extract_and_link(session_id)

        assert count == 0

    async def test_queries_correct_session(self) -> None:
        session_id = uuid4()
        mock_pool = AsyncMock()
        mock_pool.fetch.return_value = []

        with patch("theo.memory.auto_edges.db", pool=mock_pool):
            await extract_and_link(session_id)

        args = mock_pool.fetch.call_args.args
        assert args[1] == session_id


# ---------------------------------------------------------------------------
# link_memories tool
# ---------------------------------------------------------------------------


class TestLinkMemoriesTool:
    def test_tool_definition_exists(self) -> None:
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert "link_memories" in names

    def test_tool_requires_source_target_label(self) -> None:
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "link_memories")
        assert set(tool["input_schema"]["required"]) == {
            "source_id",
            "target_id",
            "label",
        }

    async def test_link_memories_creates_edge(self) -> None:
        mock_store_edge = AsyncMock(return_value=99)

        with patch("theo.memory.tools.edges.store_edge", mock_store_edge):
            result = await execute_tool(
                "link_memories",
                {
                    "source_id": 10,
                    "target_id": 20,
                    "label": "works_on",
                    "reason": "Alice works on Project X",
                },
            )

        parsed = json.loads(result)
        assert parsed == {"linked": True, "edge_id": 99}

        mock_store_edge.assert_awaited_once_with(
            source_id=10,
            target_id=20,
            label="works_on",
            weight=0.8,
            meta={"source": "llm_tool", "reason": "Alice works on Project X"},
        )

    async def test_link_memories_without_reason(self) -> None:
        mock_store_edge = AsyncMock(return_value=5)

        with patch("theo.memory.tools.edges.store_edge", mock_store_edge):
            result = await execute_tool(
                "link_memories",
                {"source_id": 1, "target_id": 2, "label": "related_to"},
            )

        parsed = json.loads(result)
        assert parsed == {"linked": True, "edge_id": 5}

        _, kwargs = mock_store_edge.call_args
        assert kwargs["meta"] == {"source": "llm_tool"}


# ---------------------------------------------------------------------------
# store_memory + record_mention integration
# ---------------------------------------------------------------------------


class TestStoreMemoryRecordsMention:
    async def test_store_memory_records_mention_when_episode_id_given(self) -> None:
        mock_store_node = AsyncMock(return_value=42)
        mock_record = AsyncMock()

        with (
            patch("theo.memory.tools.nodes.store_node", mock_store_node),
            patch("theo.memory.tools.record_mention", mock_record),
        ):
            result = await execute_tool(
                "store_memory",
                {"kind": "fact", "body": "test"},
                episode_id=10,
            )

        parsed = json.loads(result)
        assert parsed["node_id"] == 42
        mock_record.assert_awaited_once_with(10, 42)

    async def test_store_memory_skips_mention_without_episode_id(self) -> None:
        mock_store_node = AsyncMock(return_value=42)
        mock_record = AsyncMock()

        with (
            patch("theo.memory.tools.nodes.store_node", mock_store_node),
            patch("theo.memory.tools.record_mention", mock_record),
        ):
            await execute_tool(
                "store_memory",
                {"kind": "fact", "body": "test"},
            )

        mock_record.assert_not_awaited()

    async def test_multiple_store_memory_calls_record_each(self) -> None:
        node_ids = iter([10, 20])
        mock_store_node = AsyncMock(side_effect=lambda **_kw: next(node_ids))
        mock_record = AsyncMock()

        with (
            patch("theo.memory.tools.nodes.store_node", mock_store_node),
            patch("theo.memory.tools.record_mention", mock_record),
        ):
            await execute_tool(
                "store_memory",
                {"kind": "fact", "body": "first"},
                episode_id=5,
            )
            await execute_tool(
                "store_memory",
                {"kind": "fact", "body": "second"},
                episode_id=5,
            )

        assert mock_record.await_count == 2
        mock_record.assert_has_awaits([call(5, 10), call(5, 20)])
