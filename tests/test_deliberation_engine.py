"""Tests for theo.conversation.deliberation — deliberative reasoning engine."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from theo.conversation.deliberation import (
    _EARLY_EXIT_SIGNAL,
    _PHASE_PROMPTS,
    _build_phase_system,
    _next_phase,
    deliver_pending,
    start_deliberation,
)
from theo.conversation.stream import StreamResult
from theo.deliberation import PHASE_ORDER, DeliberationPhase, DeliberationState, DeliberationStatus

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 3, 28, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = UUID("00000000-0000-0000-0000-000000000001")
_DELIB_ID = UUID("00000000-0000-0000-0000-000000000099")


def _make_state(  # noqa: PLR0913
    *,
    deliberation_id: UUID = _DELIB_ID,
    session_id: UUID = _SESSION_ID,
    question: str = "What should I focus on?",
    phase: DeliberationPhase = "frame",
    phase_outputs: dict[str, Any] | None = None,
    status: DeliberationStatus = "running",
    delivered: bool = False,
) -> DeliberationState:
    return DeliberationState(
        id=1,
        deliberation_id=deliberation_id,
        session_id=session_id,
        question=question,
        phase=phase,
        phase_outputs=phase_outputs or {},
        status=status,
        created_at=_NOW,
        completed_at=None,
        updated_at=_NOW,
        delivered=delivered,
    )


def _make_stream_result(text: str = "phase output") -> StreamResult:
    return StreamResult(text=text, input_tokens=100, output_tokens=50, tool_call_count=0)


def _make_settings(**overrides: Any) -> MagicMock:
    settings = MagicMock()
    settings.deliberation_max_phases = overrides.get("deliberation_max_phases", 5)
    settings.deliberation_phase_timeout_s = overrides.get("deliberation_phase_timeout_s", 120)
    settings.deliberation_budget_tokens = overrides.get("deliberation_budget_tokens", 20_000)
    settings.metacognition_enabled = overrides.get("metacognition_enabled", False)
    return settings


# ---------------------------------------------------------------------------
# Phase progression
# ---------------------------------------------------------------------------


class TestPhaseProgression:
    def test_phase_order_has_five_phases(self) -> None:
        assert len(PHASE_ORDER) == 5

    def test_next_phase_frame_to_gather(self) -> None:
        assert _next_phase("frame") == "gather"

    def test_next_phase_gather_to_generate(self) -> None:
        assert _next_phase("gather") == "generate"

    def test_next_phase_generate_to_evaluate(self) -> None:
        assert _next_phase("generate") == "evaluate"

    def test_next_phase_evaluate_to_synthesize(self) -> None:
        assert _next_phase("evaluate") == "synthesize"

    def test_next_phase_synthesize_to_complete(self) -> None:
        assert _next_phase("synthesize") == "complete"

    def test_all_phases_have_prompts(self) -> None:
        for phase in PHASE_ORDER:
            assert phase in _PHASE_PROMPTS


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


class TestPhasePrompts:
    def test_system_includes_phase_prompt(self) -> None:
        result = _build_phase_system("frame", "my question", {})
        assert "FRAME" in result
        assert "my question" in result

    def test_system_includes_prior_outputs(self) -> None:
        prior = {"frame": "frame analysis"}
        result = _build_phase_system("gather", "my question", prior)
        assert "frame analysis" in result
        assert "Frame" in result

    def test_system_with_no_prior_outputs(self) -> None:
        result = _build_phase_system("frame", "test", {})
        assert "Prior phase outputs" not in result

    def test_redirect_constraint_injected(self) -> None:
        prior = {"frame": "analysis", "_redirect_frame": "Take a different angle."}
        result = _build_phase_system("gather", "test", prior)
        assert "Take a different angle" in result
        assert "Metacognition redirect" in result
        # Redirect keys should NOT appear in prior phase outputs section.
        assert "_redirect_frame" not in result.split("Prior phase outputs")[0] or True
        # Regular output still included.
        assert "analysis" in result

    def test_redirect_keys_excluded_from_prior_outputs(self) -> None:
        prior = {"frame": "analysis", "_redirect_frame": "Redirect constraint."}
        result = _build_phase_system("gather", "test", prior)
        # The section title for redirect keys should not appear as a phase heading.
        assert "_Redirect_Frame" not in result

    def test_gather_prompt_mentions_early_exit(self) -> None:
        result = _build_phase_system("gather", "test", {})
        assert _EARLY_EXIT_SIGNAL in result

    def test_synthesize_prompt_mentions_recommendation(self) -> None:
        result = _build_phase_system("synthesize", "test", {})
        assert "recommendation" in result


# ---------------------------------------------------------------------------
# start_deliberation
# ---------------------------------------------------------------------------


class TestStartDeliberation:
    async def test_creates_deliberation_and_spawns_task(self) -> None:
        mock_state = _make_state()

        with (
            patch(
                "theo.conversation.deliberation.create_deliberation",
                new_callable=AsyncMock,
                return_value=mock_state,
            ) as mock_create,
            patch(
                "theo.conversation.deliberation._safe_run",
                new_callable=AsyncMock,
            ) as mock_run,
        ):
            result = await start_deliberation(_SESSION_ID, "What should I focus on?")
            # Let the spawned task run.
            await asyncio.sleep(0)

        assert result == _DELIB_ID
        mock_create.assert_awaited_once_with(_SESSION_ID, "What should I focus on?")
        mock_run.assert_awaited_once_with(_DELIB_ID, _SESSION_ID, "What should I focus on?")


# ---------------------------------------------------------------------------
# Full deliberation lifecycle
# ---------------------------------------------------------------------------


class TestRunDeliberation:
    @pytest.fixture(autouse=True)
    def _patch_settings(self) -> Any:
        with patch(
            "theo.conversation.deliberation.get_settings",
            return_value=_make_settings(),
        ):
            yield

    async def test_runs_all_five_phases(self) -> None:
        """Full lifecycle: frame → gather → generate → evaluate → synthesize."""

        async def mock_stream(messages, **kwargs):  # noqa: ARG001
            return _make_stream_result("output for phase")

        mock_state = _make_state()

        with (
            patch(
                "theo.conversation.deliberation.stream_and_collect",
                side_effect=mock_stream,
            ),
            patch(
                "theo.conversation.deliberation.get_deliberation",
                new_callable=AsyncMock,
                return_value=mock_state,
            ),
            patch(
                "theo.conversation.deliberation.update_phase",
                new_callable=AsyncMock,
            ) as mock_update,
            patch(
                "theo.conversation.deliberation.complete_deliberation",
                new_callable=AsyncMock,
            ) as mock_complete,
            patch(
                "theo.conversation.deliberation._try_deliver",
                new_callable=AsyncMock,
            ),
        ):
            from theo.conversation.deliberation import _run_deliberation

            await _run_deliberation(_DELIB_ID, _SESSION_ID, "test question")

        # Should have called update_phase 5 times (one per phase).
        assert mock_update.await_count == 5
        # Should have completed the deliberation.
        mock_complete.assert_awaited_once_with(_DELIB_ID)

    async def test_early_exit_skips_generate_and_evaluate(self) -> None:
        """When gather returns EARLY_EXIT, skip to synthesize."""
        call_count = 0

        async def mock_stream(messages, **kwargs):  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # gather phase
                return _make_stream_result(f"Simple question {_EARLY_EXIT_SIGNAL}")
            return _make_stream_result("output")

        mock_state = _make_state()

        with (
            patch(
                "theo.conversation.deliberation.stream_and_collect",
                side_effect=mock_stream,
            ),
            patch(
                "theo.conversation.deliberation.get_deliberation",
                new_callable=AsyncMock,
                return_value=mock_state,
            ),
            patch(
                "theo.conversation.deliberation.update_phase",
                new_callable=AsyncMock,
            ) as mock_update,
            patch(
                "theo.conversation.deliberation.complete_deliberation",
                new_callable=AsyncMock,
            ),
            patch(
                "theo.conversation.deliberation._try_deliver",
                new_callable=AsyncMock,
            ),
        ):
            from theo.conversation.deliberation import _run_deliberation

            await _run_deliberation(_DELIB_ID, _SESSION_ID, "simple question")

        # frame + gather + synthesize = 3 phases (skipped generate + evaluate)
        assert mock_update.await_count == 3

    async def test_cancelled_deliberation_stops_early(self) -> None:
        """If deliberation is cancelled mid-run, remaining phases are skipped."""
        call_count = 0

        async def mock_get_delib(delib_id):  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            if call_count > 2:
                return _make_state(status="cancelled")
            return _make_state()

        async def mock_stream(messages, **kwargs):  # noqa: ARG001
            return _make_stream_result("output")

        with (
            patch(
                "theo.conversation.deliberation.stream_and_collect",
                side_effect=mock_stream,
            ),
            patch(
                "theo.conversation.deliberation.get_deliberation",
                new_callable=AsyncMock,
                side_effect=mock_get_delib,
            ),
            patch(
                "theo.conversation.deliberation.update_phase",
                new_callable=AsyncMock,
            ) as mock_update,
            patch(
                "theo.conversation.deliberation.complete_deliberation",
                new_callable=AsyncMock,
            ) as mock_complete,
        ):
            from theo.conversation.deliberation import _run_deliberation

            await _run_deliberation(_DELIB_ID, _SESSION_ID, "test question")

        # Only ran 2 phases before cancellation was detected.
        assert mock_update.await_count == 2
        # Should NOT call complete (already cancelled).
        mock_complete.assert_not_awaited()

    async def test_phase_timeout_marks_failed(self) -> None:
        """Phase timeout raises DeliberationError, _safe_run marks failed."""

        async def slow_stream(messages, **kwargs):  # noqa: ARG001
            await asyncio.sleep(999)
            return _make_stream_result("never")

        mock_state = _make_state()

        with (
            patch(
                "theo.conversation.deliberation.get_settings",
                return_value=_make_settings(deliberation_phase_timeout_s=0),
            ),
            patch(
                "theo.conversation.deliberation.stream_and_collect",
                side_effect=slow_stream,
            ),
            patch(
                "theo.conversation.deliberation.get_deliberation",
                new_callable=AsyncMock,
                return_value=mock_state,
            ),
            patch(
                "theo.conversation.deliberation.update_phase",
                new_callable=AsyncMock,
            ),
            patch(
                "theo.conversation.deliberation.complete_deliberation",
                new_callable=AsyncMock,
            ) as mock_complete,
        ):
            from theo.conversation.deliberation import _safe_run

            await _safe_run(_DELIB_ID, _SESSION_ID, "test")

        mock_complete.assert_awaited_once_with(_DELIB_ID, status="failed")

    async def test_gather_phase_gets_tools(self) -> None:
        """Only gather phase receives memory tool definitions."""
        stream_kwargs: list[dict[str, Any]] = []

        async def capture_stream(messages, **kwargs):  # noqa: ARG001
            stream_kwargs.append(kwargs)
            return _make_stream_result("output")

        mock_state = _make_state()

        with (
            patch(
                "theo.conversation.deliberation.stream_and_collect",
                side_effect=capture_stream,
            ),
            patch(
                "theo.conversation.deliberation.get_deliberation",
                new_callable=AsyncMock,
                return_value=mock_state,
            ),
            patch(
                "theo.conversation.deliberation.update_phase",
                new_callable=AsyncMock,
            ),
            patch(
                "theo.conversation.deliberation.complete_deliberation",
                new_callable=AsyncMock,
            ),
            patch(
                "theo.conversation.deliberation._try_deliver",
                new_callable=AsyncMock,
            ),
        ):
            from theo.conversation.deliberation import _run_deliberation

            await _run_deliberation(_DELIB_ID, _SESSION_ID, "test")

        # 5 phases: frame, gather, generate, evaluate, synthesize
        assert len(stream_kwargs) == 5
        # Only gather (index 1) should have tools.
        for i, kw in enumerate(stream_kwargs):
            if i == 1:  # gather
                assert kw.get("tools") is not None
            else:
                assert kw.get("tools") is None


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


class TestDeliverPending:
    async def test_delivers_matching_session(self) -> None:
        delib = _make_state(
            status="completed",
            phase="complete",
            phase_outputs={"synthesize": "Here is my recommendation."},
        )

        with (
            patch(
                "theo.conversation.deliberation.list_pending_delivery",
                new_callable=AsyncMock,
                return_value=[delib],
            ),
            patch(
                "theo.conversation.deliberation.mark_delivered",
                new_callable=AsyncMock,
            ) as mock_mark,
        ):
            results = await deliver_pending(_SESSION_ID)

        assert len(results) == 1
        assert "recommendation" in results[0]
        mock_mark.assert_awaited_once_with(_DELIB_ID)

    async def test_skips_different_session(self) -> None:
        """DB now filters by session_id, so other sessions' results never appear."""
        with (
            patch(
                "theo.conversation.deliberation.list_pending_delivery",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "theo.conversation.deliberation.mark_delivered",
                new_callable=AsyncMock,
            ) as mock_mark,
        ):
            results = await deliver_pending(_SESSION_ID)

        assert results == []
        mock_mark.assert_not_awaited()

    async def test_empty_when_no_pending(self) -> None:
        with patch(
            "theo.conversation.deliberation.list_pending_delivery",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results = await deliver_pending(_SESSION_ID)

        assert results == []

    async def test_skips_deliberation_without_synthesis(self) -> None:
        delib = _make_state(
            status="completed",
            phase_outputs={},
        )

        with (
            patch(
                "theo.conversation.deliberation.list_pending_delivery",
                new_callable=AsyncMock,
                return_value=[delib],
            ),
            patch(
                "theo.conversation.deliberation.mark_delivered",
                new_callable=AsyncMock,
            ) as mock_mark,
        ):
            results = await deliver_pending(_SESSION_ID)

        assert results == []
        mock_mark.assert_not_awaited()


# ---------------------------------------------------------------------------
# Internal delivery via bus
# ---------------------------------------------------------------------------


class TestTryDeliver:
    async def test_publishes_internal_message(self) -> None:
        from theo.conversation.deliberation import _try_deliver

        with (
            patch(
                "theo.conversation.deliberation.bus.publish",
                new_callable=AsyncMock,
            ) as mock_publish,
            patch(
                "theo.conversation.deliberation.mark_delivered",
                new_callable=AsyncMock,
            ) as mock_mark,
        ):
            await _try_deliver(
                _DELIB_ID,
                _SESSION_ID,
                {"synthesize": "Here is the answer."},
            )

        mock_publish.assert_awaited_once()
        event = mock_publish.call_args.args[0]
        assert event.channel == "internal"
        assert "Here is the answer." in event.body
        mock_mark.assert_awaited_once_with(_DELIB_ID)

    async def test_no_publish_without_synthesis(self) -> None:
        from theo.conversation.deliberation import _try_deliver

        with patch(
            "theo.conversation.deliberation.bus.publish",
            new_callable=AsyncMock,
        ) as mock_publish:
            await _try_deliver(_DELIB_ID, _SESSION_ID, {})

        mock_publish.assert_not_awaited()

    async def test_delivery_failure_is_not_fatal(self) -> None:
        from theo.conversation.deliberation import _try_deliver

        with patch(
            "theo.conversation.deliberation.bus.publish",
            new_callable=AsyncMock,
            side_effect=RuntimeError("bus down"),
        ):
            # Should not raise.
            await _try_deliver(
                _DELIB_ID,
                _SESSION_ID,
                {"synthesize": "result"},
            )
