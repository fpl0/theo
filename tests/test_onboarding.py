"""Tests for theo.onboarding — state machine, prompts, tool, and context integration."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from theo.config import Settings
from theo.conversation.context import assemble
from theo.memory.core import CoreDocument
from theo.memory.tools import TOOL_DEFINITIONS, execute_tool
from theo.onboarding.flow import (
    OnboardingState,
    _state_to_dict,
    advance_phase,
    complete_onboarding,
    dict_to_state,
    get_onboarding_state,
    is_onboarding_completed,
    start_onboarding,
)
from theo.onboarding.prompts import PHASES, get_phase_system_prompt

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _context_doc(body: dict[str, Any] | None = None) -> CoreDocument:
    return CoreDocument(
        label="context",
        body=body if body is not None else {"current_task": None, "focus": None},
        version=1,
        updated_at=_NOW,
    )


def _make_state(
    *,
    phase: str = "welcome",
    phase_index: int = 0,
    completed_phases: tuple[str, ...] = (),
) -> OnboardingState:
    return OnboardingState(
        phase=phase,
        phase_index=phase_index,
        started_at="2026-01-15T12:00:00+00:00",
        completed_phases=completed_phases,
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


class TestPhases:
    def test_phase_count(self) -> None:
        assert len(PHASES) == 8

    def test_phase_order(self) -> None:
        assert PHASES == (
            "welcome",
            "values",
            "personality",
            "communication",
            "energy",
            "goals",
            "boundaries",
            "wrap_up",
        )

    def test_each_phase_has_prompt(self) -> None:
        for phase in PHASES:
            prompt = get_phase_system_prompt(phase)
            assert isinstance(prompt, str)
            assert len(prompt) > 50

    def test_unknown_phase_raises(self) -> None:
        with pytest.raises(KeyError):
            get_phase_system_prompt("nonexistent")

    def test_values_prompt_mentions_schwartz(self) -> None:
        prompt = get_phase_system_prompt("values")
        assert "schwartz" in prompt.lower() or "Schwartz" in prompt

    def test_personality_prompt_mentions_big_five(self) -> None:
        prompt = get_phase_system_prompt("personality")
        assert "big_five" in prompt.lower() or "Big Five" in prompt

    def test_prompts_instruct_tool_usage(self) -> None:
        for phase in PHASES:
            if phase in ("welcome", "wrap_up"):
                continue
            prompt = get_phase_system_prompt(phase)
            assert "update_user_model" in prompt

    def test_prompts_instruct_advance(self) -> None:
        for phase in PHASES:
            prompt = get_phase_system_prompt(phase)
            assert "advance_onboarding" in prompt


# ---------------------------------------------------------------------------
# State serialization
# ---------------------------------------------------------------------------


class TestStateSerialization:
    def test_round_trip(self) -> None:
        state = _make_state(phase="values", phase_index=1, completed_phases=("welcome",))
        serialized = _state_to_dict(state)
        restored = dict_to_state(serialized)
        assert restored.phase == state.phase
        assert restored.phase_index == state.phase_index
        assert restored.started_at == state.started_at
        assert restored.completed_phases == state.completed_phases

    def test_serializes_completed_phases_as_list(self) -> None:
        state = _make_state(completed_phases=("welcome", "values"))
        serialized = _state_to_dict(state)
        assert isinstance(serialized["completed_phases"], list)

    def test_deserializes_completed_phases_as_tuple(self) -> None:
        data = {
            "phase": "welcome",
            "phase_index": 0,
            "started_at": "2026-01-15T12:00:00+00:00",
            "completed_phases": ["welcome"],
        }
        state = dict_to_state(data)
        assert isinstance(state.completed_phases, tuple)

    def test_rejects_out_of_range_phase_index(self) -> None:
        data = {
            "phase": "welcome",
            "phase_index": 99,
            "started_at": "2026-01-15T12:00:00+00:00",
            "completed_phases": [],
        }
        with pytest.raises(ValueError, match="out of range"):
            dict_to_state(data)

    def test_rejects_mismatched_phase_and_index(self) -> None:
        data = {
            "phase": "values",
            "phase_index": 0,
            "started_at": "2026-01-15T12:00:00+00:00",
            "completed_phases": [],
        }
        with pytest.raises(ValueError, match="does not match"):
            dict_to_state(data)

    def test_rejects_missing_key(self) -> None:
        data = {"phase": "welcome"}
        with pytest.raises(KeyError):
            dict_to_state(data)


# ---------------------------------------------------------------------------
# get_onboarding_state
# ---------------------------------------------------------------------------


class TestGetOnboardingState:
    async def test_returns_none_when_no_key(self) -> None:
        mock_read_one = AsyncMock(return_value=_context_doc())
        with patch("theo.onboarding.flow.core.read_one", mock_read_one):
            result = await get_onboarding_state()
        assert result is None

    async def test_returns_state_when_present(self) -> None:
        state_dict = _state_to_dict(_make_state(phase="values", phase_index=1))
        doc = _context_doc({"current_task": None, "onboarding": state_dict})
        mock_read_one = AsyncMock(return_value=doc)
        with patch("theo.onboarding.flow.core.read_one", mock_read_one):
            result = await get_onboarding_state()
        assert result is not None
        assert result.phase == "values"
        assert result.phase_index == 1

    async def test_returns_none_for_non_dict_value(self) -> None:
        doc = _context_doc({"onboarding": "invalid"})
        mock_read_one = AsyncMock(return_value=doc)
        with patch("theo.onboarding.flow.core.read_one", mock_read_one):
            result = await get_onboarding_state()
        assert result is None


# ---------------------------------------------------------------------------
# is_onboarding_completed
# ---------------------------------------------------------------------------


class TestIsOnboardingCompleted:
    async def test_false_when_never_started(self) -> None:
        mock_read_one = AsyncMock(return_value=_context_doc())
        with patch("theo.onboarding.flow.core.read_one", mock_read_one):
            assert await is_onboarding_completed() is False

    async def test_true_when_completed(self) -> None:
        doc = _context_doc({"onboarding_completed": True})
        mock_read_one = AsyncMock(return_value=doc)
        with patch("theo.onboarding.flow.core.read_one", mock_read_one):
            assert await is_onboarding_completed() is True


# ---------------------------------------------------------------------------
# start_onboarding
# ---------------------------------------------------------------------------


class TestStartOnboarding:
    async def test_creates_initial_state(self) -> None:
        mock_read_one = AsyncMock(return_value=_context_doc())
        mock_update = AsyncMock(return_value=2)
        with (
            patch("theo.onboarding.flow.core.read_one", mock_read_one),
            patch("theo.onboarding.flow.core.update", mock_update),
        ):
            state = await start_onboarding()

        assert state.phase == "welcome"
        assert state.phase_index == 0
        assert state.completed_phases == ()

    async def test_persists_to_context(self) -> None:
        mock_read_one = AsyncMock(return_value=_context_doc())
        mock_update = AsyncMock(return_value=2)
        with (
            patch("theo.onboarding.flow.core.read_one", mock_read_one),
            patch("theo.onboarding.flow.core.update", mock_update),
        ):
            await start_onboarding()

        mock_update.assert_awaited_once()
        call_kwargs = mock_update.call_args
        assert call_kwargs.args[0] == "context"
        body = call_kwargs.kwargs["body"]
        assert "onboarding" in body
        assert body["onboarding"]["phase"] == "welcome"

    async def test_clears_completed_flag_on_restart(self) -> None:
        """Starting onboarding must clear a stale onboarding_completed flag."""
        doc = _context_doc({"current_task": None, "onboarding_completed": True})
        mock_read_one = AsyncMock(return_value=doc)
        mock_update = AsyncMock(return_value=2)
        with (
            patch("theo.onboarding.flow.core.read_one", mock_read_one),
            patch("theo.onboarding.flow.core.update", mock_update),
        ):
            await start_onboarding()

        body = mock_update.call_args.kwargs["body"]
        assert "onboarding_completed" not in body
        assert "onboarding" in body


# ---------------------------------------------------------------------------
# advance_phase
# ---------------------------------------------------------------------------


class TestAdvancePhase:
    async def test_moves_to_next_phase(self) -> None:
        state = _make_state(phase="welcome", phase_index=0)
        mock_read_one = AsyncMock(return_value=_context_doc())
        mock_update = AsyncMock(return_value=2)
        with (
            patch("theo.onboarding.flow.core.read_one", mock_read_one),
            patch("theo.onboarding.flow.core.update", mock_update),
        ):
            new_state = await advance_phase(state)

        assert new_state is not None
        assert new_state.phase == "values"
        assert new_state.phase_index == 1
        assert "welcome" in new_state.completed_phases

    async def test_completes_after_last_phase(self) -> None:
        state = _make_state(
            phase="wrap_up",
            phase_index=7,
            completed_phases=PHASES[:-1],
        )
        mock_read_one = AsyncMock(return_value=_context_doc({"onboarding": {}}))
        mock_update = AsyncMock(return_value=2)
        with (
            patch("theo.onboarding.flow.core.read_one", mock_read_one),
            patch("theo.onboarding.flow.core.update", mock_update),
        ):
            result = await advance_phase(state)

        assert result is None

    async def test_preserves_started_at(self) -> None:
        state = _make_state(phase="values", phase_index=1, completed_phases=("welcome",))
        mock_read_one = AsyncMock(return_value=_context_doc())
        mock_update = AsyncMock(return_value=2)
        with (
            patch("theo.onboarding.flow.core.read_one", mock_read_one),
            patch("theo.onboarding.flow.core.update", mock_update),
        ):
            new_state = await advance_phase(state)

        assert new_state is not None
        assert new_state.started_at == state.started_at

    async def test_full_lifecycle_through_all_phases(self) -> None:
        """Walk through all 8 phases to verify the complete state machine."""
        mock_read_one = AsyncMock(return_value=_context_doc())
        mock_update = AsyncMock(return_value=2)

        with (
            patch("theo.onboarding.flow.core.read_one", mock_read_one),
            patch("theo.onboarding.flow.core.update", mock_update),
        ):
            state = await start_onboarding()
            assert state.phase == PHASES[0]

            for i in range(len(PHASES) - 1):
                new_state = await advance_phase(state)
                assert new_state is not None
                assert new_state.phase == PHASES[i + 1]
                assert new_state.phase_index == i + 1
                assert len(new_state.completed_phases) == i + 1
                state = new_state

            # Final advance completes onboarding.
            result = await advance_phase(state)
            assert result is None


# ---------------------------------------------------------------------------
# complete_onboarding
# ---------------------------------------------------------------------------


class TestCompleteOnboarding:
    async def test_removes_onboarding_key(self) -> None:
        doc = _context_doc({"current_task": None, "onboarding": {"phase": "wrap_up"}})
        mock_read_one = AsyncMock(return_value=doc)
        mock_update = AsyncMock(return_value=2)
        with (
            patch("theo.onboarding.flow.core.read_one", mock_read_one),
            patch("theo.onboarding.flow.core.update", mock_update),
        ):
            await complete_onboarding()

        body = mock_update.call_args.kwargs["body"]
        assert "onboarding" not in body

    async def test_sets_completed_flag(self) -> None:
        doc = _context_doc({"onboarding": {"phase": "wrap_up"}})
        mock_read_one = AsyncMock(return_value=doc)
        mock_update = AsyncMock(return_value=2)
        with (
            patch("theo.onboarding.flow.core.read_one", mock_read_one),
            patch("theo.onboarding.flow.core.update", mock_update),
        ):
            await complete_onboarding()

        body = mock_update.call_args.kwargs["body"]
        assert body["onboarding_completed"] is True

    async def test_preserves_other_context_keys(self) -> None:
        doc = _context_doc(
            {
                "current_task": "something",
                "onboarding": {"phase": "wrap_up"},
            }
        )
        mock_read_one = AsyncMock(return_value=doc)
        mock_update = AsyncMock(return_value=2)
        with (
            patch("theo.onboarding.flow.core.read_one", mock_read_one),
            patch("theo.onboarding.flow.core.update", mock_update),
        ):
            await complete_onboarding()

        body = mock_update.call_args.kwargs["body"]
        assert body["current_task"] == "something"


# ---------------------------------------------------------------------------
# advance_onboarding tool
# ---------------------------------------------------------------------------


class TestAdvanceOnboardingTool:
    def test_tool_defined(self) -> None:
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert "advance_onboarding" in names

    def test_tool_requires_summary(self) -> None:
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "advance_onboarding")
        assert "summary" in tool["input_schema"]["required"]

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
            "start_deliberation",
        }
        assert names == expected

    async def test_returns_next_phase(self) -> None:
        state = _make_state(phase="welcome", phase_index=0)
        new_state = _make_state(phase="values", phase_index=1, completed_phases=("welcome",))

        with (
            patch(
                "theo.memory.tools.onboarding_flow.get_onboarding_state",
                AsyncMock(return_value=state),
            ),
            patch(
                "theo.memory.tools.onboarding_flow.advance_phase",
                AsyncMock(return_value=new_state),
            ),
        ):
            result = await execute_tool("advance_onboarding", {"summary": "intro done"})

        parsed = json.loads(result)
        assert parsed["phase"] == "values"
        assert parsed["phase_index"] == 1

    async def test_returns_completed_on_last_phase(self) -> None:
        state = _make_state(phase="wrap_up", phase_index=7)

        with (
            patch(
                "theo.memory.tools.onboarding_flow.get_onboarding_state",
                AsyncMock(return_value=state),
            ),
            patch(
                "theo.memory.tools.onboarding_flow.advance_phase",
                AsyncMock(return_value=None),
            ),
        ):
            result = await execute_tool("advance_onboarding", {"summary": "all done"})

        parsed = json.loads(result)
        assert parsed["completed"] is True

    async def test_returns_error_when_no_active_session(self) -> None:
        with patch(
            "theo.memory.tools.onboarding_flow.get_onboarding_state",
            AsyncMock(return_value=None),
        ):
            result = await execute_tool("advance_onboarding", {"summary": "test"})

        parsed = json.loads(result)
        assert "error" in parsed


# ---------------------------------------------------------------------------
# Context assembly integration
# ---------------------------------------------------------------------------


_MOCK_DELIVER = "theo.conversation.context.assembly.deliver_pending"


class TestContextOnboardingIntegration:
    async def test_injects_phase_prompt_when_active(self) -> None:
        state = _make_state(phase="values", phase_index=1)
        all_docs = {
            "persona": _context_doc_as("persona", {"summary": "Theo"}),
            "goals": _context_doc_as("goals", {"active": []}),
            "user_model": _context_doc_as("user_model", {"preferences": {}}),
            "context": _context_doc_as("context", {"onboarding": _state_to_dict(state)}),
        }

        with (
            patch(
                "theo.conversation.context.assembly.core.read_all",
                AsyncMock(return_value=all_docs),
            ),
            patch("theo.conversation.context.assembly.hybrid_search", AsyncMock(return_value=[])),
            patch("theo.conversation.context.assembly.list_episodes", AsyncMock(return_value=[])),
            patch("theo.conversation.context.assembly.get_settings", return_value=_settings()),
            patch(_MOCK_DELIVER, AsyncMock(return_value=[])),
        ):
            ctx = await assemble(
                session_id=UUID("00000000-0000-0000-0000-000000000001"),
                latest_message="hello",
            )

        assert "## Onboarding (Phase 2: Values)" in ctx.system_prompt
        assert "Schwartz" in ctx.system_prompt
        assert ctx.token_estimate > 0

    async def test_no_injection_when_not_onboarding(self) -> None:
        all_docs = {
            "persona": _context_doc_as("persona", {"summary": "Theo"}),
            "goals": _context_doc_as("goals", {"active": []}),
            "user_model": _context_doc_as("user_model", {"preferences": {}}),
            "context": _context_doc_as("context", {"current_task": None}),
        }

        with (
            patch(
                "theo.conversation.context.assembly.core.read_all",
                AsyncMock(return_value=all_docs),
            ),
            patch("theo.conversation.context.assembly.hybrid_search", AsyncMock(return_value=[])),
            patch("theo.conversation.context.assembly.list_episodes", AsyncMock(return_value=[])),
            patch("theo.conversation.context.assembly.get_settings", return_value=_settings()),
            patch(_MOCK_DELIVER, AsyncMock(return_value=[])),
        ):
            ctx = await assemble(
                session_id=UUID("00000000-0000-0000-0000-000000000001"),
                latest_message="hello",
            )

        assert "Onboarding" not in ctx.system_prompt

    async def test_corrupted_onboarding_state_skipped_gracefully(self) -> None:
        """If onboarding state is corrupted, context assembly should skip it."""
        all_docs = {
            "persona": _context_doc_as("persona", {"summary": "Theo"}),
            "context": _context_doc_as(
                "context",
                {"onboarding": {"phase": "values", "phase_index": 99}},
            ),
        }

        with (
            patch(
                "theo.conversation.context.assembly.core.read_all",
                AsyncMock(return_value=all_docs),
            ),
            patch("theo.conversation.context.assembly.hybrid_search", AsyncMock(return_value=[])),
            patch("theo.conversation.context.assembly.list_episodes", AsyncMock(return_value=[])),
            patch("theo.conversation.context.assembly.get_settings", return_value=_settings()),
            patch(_MOCK_DELIVER, AsyncMock(return_value=[])),
        ):
            ctx = await assemble(
                session_id=UUID("00000000-0000-0000-0000-000000000001"),
                latest_message="hello",
            )

        assert "Onboarding" not in ctx.system_prompt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    """Create a real Settings instance with test defaults."""
    defaults: dict[str, Any] = {
        "database_url": "postgresql://x:x@localhost/x",
        "anthropic_api_key": "sk-test",
        "_env_file": None,
    }
    return Settings(**(defaults | overrides))


def _context_doc_as(label: str, body: dict[str, Any]) -> CoreDocument:
    return CoreDocument(
        label=label,
        body=body,
        version=1,
        updated_at=_NOW,
    )
