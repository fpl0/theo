"""Tests for theo.conversation.metacognition — metacognitive monitor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from theo.conversation.metacognition import (
    MonitorDecision,
    _cosine_similarity,
    _detect_diminishing_returns,
    _detect_drift,
    _detect_overconfidence,
    _detect_spinning,
    _previous_phase,
    extract_node_ids,
    monitor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vec(values: list[float]) -> np.ndarray:
    """Create an L2-normalised float32 vector."""
    v = np.array(values, dtype=np.float32)
    return v / np.linalg.norm(v)


def _make_settings(**overrides: object) -> MagicMock:
    settings = MagicMock()
    settings.metacognition_enabled = overrides.get("metacognition_enabled", True)
    settings.metacognition_spinning_threshold = overrides.get(
        "metacognition_spinning_threshold", 0.85
    )
    settings.metacognition_drift_threshold = overrides.get("metacognition_drift_threshold", 0.7)
    settings.metacognition_min_evidence_for_high_confidence = overrides.get(
        "metacognition_min_evidence_for_high_confidence", 3
    )
    return settings


# ---------------------------------------------------------------------------
# extract_node_ids
# ---------------------------------------------------------------------------


class TestExtractNodeIds:
    def test_extracts_id_from_json(self) -> None:
        text = 'Found: {"id": 42, "kind": "fact", "body": "test"}'
        assert extract_node_ids(text) == [42]

    def test_extracts_node_id_from_json(self) -> None:
        text = '{"stored": true, "node_id": 7}'
        assert extract_node_ids(text) == [7]

    def test_extracts_multiple(self) -> None:
        text = '{"id": 1}, {"id": 2}, {"id": 3}'
        assert extract_node_ids(text) == [1, 2, 3]

    def test_empty_on_no_match(self) -> None:
        assert extract_node_ids("no nodes here") == []

    def test_handles_whitespace_variants(self) -> None:
        text = '"id":  99'
        assert extract_node_ids(text) == [99]


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors(self) -> None:
        v = _vec([1.0, 0.0, 0.0])
        assert _cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-6)

    def test_orthogonal_vectors(self) -> None:
        a = _vec([1.0, 0.0, 0.0])
        b = _vec([0.0, 1.0, 0.0])
        assert _cosine_similarity(a, b) == pytest.approx(0.0, abs=1e-6)

    def test_similar_vectors(self) -> None:
        a = _vec([1.0, 0.1, 0.0])
        b = _vec([1.0, 0.2, 0.0])
        assert _cosine_similarity(a, b) > 0.99


# ---------------------------------------------------------------------------
# _previous_phase
# ---------------------------------------------------------------------------


class TestPreviousPhase:
    def test_frame_has_no_previous(self) -> None:
        assert _previous_phase("frame") is None

    def test_gather_previous_is_frame(self) -> None:
        assert _previous_phase("gather") == "frame"

    def test_synthesize_previous_is_evaluate(self) -> None:
        assert _previous_phase("synthesize") == "evaluate"

    def test_unknown_phase_returns_none(self) -> None:
        assert _previous_phase("unknown") is None


# ---------------------------------------------------------------------------
# Spinning detection
# ---------------------------------------------------------------------------


class TestDetectSpinning:
    def test_detects_identical_outputs(self) -> None:
        v = _vec([1.0, 0.0, 0.0])
        embeddings = {"frame": v, "gather": v}
        result = _detect_spinning(embeddings, "gather", "frame", threshold=0.85)
        assert result is not None
        assert result > 0.85

    def test_no_spinning_for_different_outputs(self) -> None:
        embeddings = {
            "frame": _vec([1.0, 0.0, 0.0]),
            "gather": _vec([0.0, 1.0, 0.0]),
        }
        result = _detect_spinning(embeddings, "gather", "frame", threshold=0.85)
        assert result is None

    def test_no_previous_phase(self) -> None:
        embeddings = {"frame": _vec([1.0, 0.0, 0.0])}
        result = _detect_spinning(embeddings, "frame", None, threshold=0.85)
        assert result is None

    def test_missing_current_embedding(self) -> None:
        embeddings = {"frame": _vec([1.0, 0.0, 0.0])}
        result = _detect_spinning(embeddings, "gather", "frame", threshold=0.85)
        assert result is None


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


class TestDetectDrift:
    def test_detects_drifted_output(self) -> None:
        question = _vec([1.0, 0.0, 0.0])
        embeddings = {"generate": _vec([0.0, 1.0, 0.0])}
        result = _detect_drift(embeddings, question, "generate", threshold=0.7)
        assert result is not None
        assert result < 0.7

    def test_no_drift_for_aligned_output(self) -> None:
        question = _vec([1.0, 0.1, 0.0])
        embeddings = {"generate": _vec([1.0, 0.2, 0.0])}
        result = _detect_drift(embeddings, question, "generate", threshold=0.7)
        assert result is None

    def test_missing_current_embedding(self) -> None:
        question = _vec([1.0, 0.0, 0.0])
        result = _detect_drift({}, question, "generate", threshold=0.7)
        assert result is None


# ---------------------------------------------------------------------------
# Overconfidence detection
# ---------------------------------------------------------------------------


class TestDetectOverconfidence:
    def test_detects_high_confidence_low_evidence(self) -> None:
        outputs = {"evaluate": "I have high confidence this is correct."}
        assert _detect_overconfidence(outputs, "evaluate", [1, 2], min_evidence=3)

    def test_no_flag_with_enough_evidence(self) -> None:
        outputs = {"evaluate": "I have high confidence this is correct."}
        assert not _detect_overconfidence(outputs, "evaluate", [1, 2, 3], min_evidence=3)

    def test_no_flag_without_confidence_claim(self) -> None:
        outputs = {"evaluate": "This approach has some merit."}
        assert not _detect_overconfidence(outputs, "evaluate", [1], min_evidence=3)

    def test_skips_non_evaluate_phases(self) -> None:
        outputs = {"frame": "I am very confident."}
        assert not _detect_overconfidence(outputs, "frame", [], min_evidence=3)

    def test_deduplicates_node_ids(self) -> None:
        outputs = {"synthesize": "I strongly recommend this approach."}
        # 3 entries but only 2 distinct.
        assert _detect_overconfidence(outputs, "synthesize", [1, 1, 2], min_evidence=3)

    def test_synthesize_phase_checked(self) -> None:
        outputs = {"synthesize": "I am very confident in this answer."}
        assert _detect_overconfidence(outputs, "synthesize", [], min_evidence=3)


# ---------------------------------------------------------------------------
# Diminishing returns detection
# ---------------------------------------------------------------------------


class TestDetectDiminishingReturns:
    def test_no_novel_nodes(self) -> None:
        assert _detect_diminishing_returns("generate", [1, 2], [1, 2, 3])

    def test_has_novel_nodes(self) -> None:
        assert not _detect_diminishing_returns("generate", [1, 2, 4], [1, 2, 3])

    def test_skips_non_generative_phases(self) -> None:
        assert not _detect_diminishing_returns("frame", [1], [1])
        assert not _detect_diminishing_returns("gather", [1], [1])
        assert not _detect_diminishing_returns("synthesize", [1], [1])

    def test_no_prior_nodes(self) -> None:
        assert not _detect_diminishing_returns("generate", [1], [])

    def test_empty_current_not_flagged(self) -> None:
        # No current nodes = nothing to compare, not diminishing returns.
        assert not _detect_diminishing_returns("generate", [], [1, 2])


# ---------------------------------------------------------------------------
# Full monitor integration
# ---------------------------------------------------------------------------


class TestMonitor:
    @pytest.fixture(autouse=True)
    def _patch_settings(self):
        with patch(
            "theo.conversation.metacognition.get_settings",
            return_value=_make_settings(),
        ):
            yield

    async def test_continue_when_no_pathology(self) -> None:
        question_emb = _vec([1.0, 0.0, 0.0])
        phase_outputs = {"frame": "Defined the question clearly."}

        # Return a vector aligned with the question — no drift.
        mock_embed = AsyncMock(return_value=np.array([_vec([1.0, 0.1, 0.0])], dtype=np.float32))
        with patch("theo.conversation.metacognition.embedder") as mock_embedder:
            mock_embedder.embed = mock_embed
            result = await monitor(
                question_embedding=question_emb,
                phase_outputs=phase_outputs,
                current_phase="frame",
                nodes_referenced=[],
            )

        assert result.action == "continue"
        assert result.redirect_prompt is None

    async def test_spinning_triggers_redirect(self) -> None:
        question_emb = _vec([1.0, 0.0, 0.0])
        identical_vec = _vec([0.5, 0.5, 0.0])
        phase_outputs = {"frame": "Analysis A", "gather": "Analysis A repeated"}

        mock_embed = AsyncMock(
            return_value=np.array([identical_vec, identical_vec], dtype=np.float32)
        )
        with patch("theo.conversation.metacognition.embedder") as mock_embedder:
            mock_embedder.embed = mock_embed
            result = await monitor(
                question_embedding=question_emb,
                phase_outputs=phase_outputs,
                current_phase="gather",
                nodes_referenced=[],
            )

        assert result.action == "redirect"
        assert "Spinning" in result.reasoning
        assert result.redirect_prompt is not None

    async def test_drift_triggers_redirect(self) -> None:
        question_emb = _vec([1.0, 0.0, 0.0])
        drifted_vec = _vec([0.0, 1.0, 0.0])
        frame_vec = _vec([0.8, 0.2, 0.0])

        phase_outputs = {"frame": "On topic", "generate": "Completely off topic"}

        mock_embed = AsyncMock(return_value=np.array([frame_vec, drifted_vec], dtype=np.float32))
        with patch("theo.conversation.metacognition.embedder") as mock_embedder:
            mock_embedder.embed = mock_embed
            result = await monitor(
                question_embedding=question_emb,
                phase_outputs=phase_outputs,
                current_phase="generate",
                nodes_referenced=[],
            )

        assert result.action == "redirect"
        assert "drift" in result.reasoning.lower()

    async def test_overconfidence_triggers_escalate(self) -> None:
        question_emb = _vec([1.0, 0.0, 0.0])
        phase_outputs = {"evaluate": "I have high confidence this is the answer."}
        eval_vec = _vec([0.9, 0.1, 0.0])

        mock_embed = AsyncMock(return_value=np.array([eval_vec], dtype=np.float32))
        with patch("theo.conversation.metacognition.embedder") as mock_embedder:
            mock_embedder.embed = mock_embed
            result = await monitor(
                question_embedding=question_emb,
                phase_outputs=phase_outputs,
                current_phase="evaluate",
                nodes_referenced=[1],  # Only 1 node, below threshold of 3.
            )

        assert result.action == "escalate"
        assert "Overconfidence" in result.reasoning

    async def test_diminishing_returns_triggers_abort(self) -> None:
        question_emb = _vec([1.0, 0.0, 0.0])
        phase_outputs = {
            "frame": "Framed",
            "gather": "Found nodes 1, 2",
            "generate": "Reused same info from nodes 1, 2",
        }
        # Vectors: drift > 0.7 and spinning < 0.85.
        vecs = np.array(
            [_vec([1.0, 0.0, 0.5]), _vec([1.0, 0.5, 0.0]), _vec([1.0, 0.0, -0.5])],
            dtype=np.float32,
        )

        mock_embed = AsyncMock(return_value=vecs)
        with patch("theo.conversation.metacognition.embedder") as mock_embedder:
            mock_embedder.embed = mock_embed
            result = await monitor(
                question_embedding=question_emb,
                phase_outputs=phase_outputs,
                current_phase="generate",
                nodes_referenced=[1, 2],
                prior_nodes_referenced=[1, 2, 3],
            )

        assert result.action == "abort"
        assert "Diminishing" in result.reasoning

    async def test_empty_phase_outputs(self) -> None:
        question_emb = _vec([1.0, 0.0, 0.0])

        mock_embed = AsyncMock(return_value=np.empty((0, 3), dtype=np.float32))
        with patch("theo.conversation.metacognition.embedder") as mock_embedder:
            mock_embedder.embed = mock_embed
            result = await monitor(
                question_embedding=question_emb,
                phase_outputs={},
                current_phase="frame",
                nodes_referenced=[],
            )

        assert result.action == "continue"

    async def test_spinning_priority_over_drift(self) -> None:
        """Spinning is checked before drift — if both trigger, spinning wins."""
        question_emb = _vec([1.0, 0.0, 0.0])
        # Both phases identical AND drifted from question.
        drifted = _vec([0.0, 1.0, 0.0])
        phase_outputs = {"frame": "off topic A", "gather": "off topic A again"}

        mock_embed = AsyncMock(return_value=np.array([drifted, drifted], dtype=np.float32))
        with patch("theo.conversation.metacognition.embedder") as mock_embedder:
            mock_embedder.embed = mock_embed
            result = await monitor(
                question_embedding=question_emb,
                phase_outputs=phase_outputs,
                current_phase="gather",
                nodes_referenced=[],
            )

        assert result.action == "redirect"
        assert "Spinning" in result.reasoning


# ---------------------------------------------------------------------------
# Deliberation integration — metacognition in the phase loop
# ---------------------------------------------------------------------------


def _stream_result(text: str = "output") -> object:
    from theo.conversation.stream import StreamResult

    return StreamResult(
        text=text,
        input_tokens=100,
        output_tokens=50,
        tool_call_count=0,
    )


class TestDeliberationMetacognitionIntegration:
    """Tests that the deliberation engine correctly calls metacognition."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self):
        settings = _make_settings()
        settings.deliberation_phase_timeout_s = 120
        settings.deliberation_budget_tokens = 20_000
        with patch(
            "theo.conversation.deliberation.get_settings",
            return_value=settings,
        ):
            yield

    def _make_state(self, **overrides):
        from datetime import UTC, datetime
        from uuid import UUID

        from theo.deliberation import DeliberationState

        defaults = {
            "id": 1,
            "deliberation_id": UUID("00000000-0000-0000-0000-000000000099"),
            "session_id": UUID("00000000-0000-0000-0000-000000000001"),
            "question": "test question",
            "phase": "frame",
            "phase_outputs": {},
            "status": "running",
            "created_at": datetime(2026, 3, 28, 12, 0, 0, tzinfo=UTC),
            "completed_at": None,
            "updated_at": datetime(2026, 3, 28, 12, 0, 0, tzinfo=UTC),
            "delivered": False,
        }
        defaults.update(overrides)
        return DeliberationState(**defaults)

    async def test_abort_stops_deliberation(self) -> None:
        """When monitor returns abort, deliberation completes early."""

        from uuid import UUID

        session_id = UUID("00000000-0000-0000-0000-000000000001")
        delib_id = UUID("00000000-0000-0000-0000-000000000099")

        async def mock_stream(messages, **kwargs):  # noqa: ARG001
            return _stream_result()

        abort_decision = MonitorDecision(
            action="abort",
            reasoning="Diminishing returns detected.",
        )

        with (
            patch(
                "theo.conversation.deliberation.stream_and_collect",
                side_effect=mock_stream,
            ),
            patch(
                "theo.conversation.deliberation.get_deliberation",
                new_callable=AsyncMock,
                return_value=self._make_state(),
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
                "theo.conversation.deliberation.monitor",
                new_callable=AsyncMock,
                return_value=abort_decision,
            ),
            patch(
                "theo.conversation.deliberation.embedder",
            ) as mock_embedder,
            patch(
                "theo.conversation.deliberation._try_deliver",
                new_callable=AsyncMock,
            ),
        ):
            mock_embedder.embed_one = AsyncMock(
                return_value=np.array([1.0, 0.0, 0.0], dtype=np.float32)
            )

            from theo.conversation.deliberation import _run_deliberation

            await _run_deliberation(delib_id, session_id, "test")

        # Only 1 phase ran before abort.
        assert mock_update.await_count == 1
        # Deliberation gets cancelled by metacognition.
        mock_complete.assert_awaited_once_with(delib_id, status="cancelled")

    async def test_escalate_publishes_alert_and_continues(self) -> None:
        """Escalation publishes an alert but doesn't stop the deliberation."""
        from uuid import UUID

        session_id = UUID("00000000-0000-0000-0000-000000000001")
        delib_id = UUID("00000000-0000-0000-0000-000000000099")

        call_count = 0

        async def mock_monitor(**kwargs):  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MonitorDecision(action="escalate", reasoning="Overconfidence.")
            return MonitorDecision(action="continue", reasoning="OK.")

        async def mock_stream(messages, **kwargs):  # noqa: ARG001
            return _stream_result()

        with (
            patch(
                "theo.conversation.deliberation.stream_and_collect",
                side_effect=mock_stream,
            ),
            patch(
                "theo.conversation.deliberation.get_deliberation",
                new_callable=AsyncMock,
                return_value=self._make_state(),
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
                "theo.conversation.deliberation.monitor",
                new_callable=AsyncMock,
                side_effect=mock_monitor,
            ),
            patch(
                "theo.conversation.deliberation.embedder",
            ) as mock_embedder,
            patch(
                "theo.conversation.deliberation._try_deliver",
                new_callable=AsyncMock,
            ),
            patch(
                "theo.conversation.deliberation._publish_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
        ):
            mock_embedder.embed_one = AsyncMock(
                return_value=np.array([1.0, 0.0, 0.0], dtype=np.float32)
            )

            from theo.conversation.deliberation import _run_deliberation

            await _run_deliberation(delib_id, session_id, "test")

        # Alert was published for the escalation.
        mock_alert.assert_awaited_once()

    async def test_redirect_stores_prompt_in_phase_outputs(self) -> None:
        """Redirect injects constraint for the next phase's system prompt."""
        from uuid import UUID

        session_id = UUID("00000000-0000-0000-0000-000000000001")
        delib_id = UUID("00000000-0000-0000-0000-000000000099")

        call_count = 0

        async def mock_monitor(**kwargs):  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MonitorDecision(
                    action="redirect",
                    reasoning="Spinning detected.",
                    redirect_prompt="Take a different angle.",
                )
            return MonitorDecision(action="continue", reasoning="OK.")

        captured_systems: list[str] = []

        async def mock_stream(messages, **kwargs):  # noqa: ARG001
            if kwargs.get("system"):
                captured_systems.append(kwargs["system"])
            return _stream_result()

        with (
            patch(
                "theo.conversation.deliberation.stream_and_collect",
                side_effect=mock_stream,
            ),
            patch(
                "theo.conversation.deliberation.get_deliberation",
                new_callable=AsyncMock,
                return_value=self._make_state(),
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
                "theo.conversation.deliberation.monitor",
                new_callable=AsyncMock,
                side_effect=mock_monitor,
            ),
            patch(
                "theo.conversation.deliberation.embedder",
            ) as mock_embedder,
            patch(
                "theo.conversation.deliberation._try_deliver",
                new_callable=AsyncMock,
            ),
        ):
            mock_embedder.embed_one = AsyncMock(
                return_value=np.array([1.0, 0.0, 0.0], dtype=np.float32)
            )

            from theo.conversation.deliberation import _run_deliberation

            await _run_deliberation(delib_id, session_id, "test")

        # The second phase's system prompt should contain the redirect.
        assert any("Take a different angle" in s for s in captured_systems)
        # Redirect should be in a metacognition section.
        assert any("Metacognition redirect" in s for s in captured_systems)

    async def test_monitor_failure_does_not_crash_deliberation(self) -> None:
        """If the monitor raises, deliberation continues."""
        from uuid import UUID

        session_id = UUID("00000000-0000-0000-0000-000000000001")
        delib_id = UUID("00000000-0000-0000-0000-000000000099")

        async def mock_stream(messages, **kwargs):  # noqa: ARG001
            return _stream_result()

        with (
            patch(
                "theo.conversation.deliberation.stream_and_collect",
                side_effect=mock_stream,
            ),
            patch(
                "theo.conversation.deliberation.get_deliberation",
                new_callable=AsyncMock,
                return_value=self._make_state(),
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
                "theo.conversation.deliberation.monitor",
                new_callable=AsyncMock,
                side_effect=RuntimeError("monitor crashed"),
            ),
            patch(
                "theo.conversation.deliberation.embedder",
            ) as mock_embedder,
            patch(
                "theo.conversation.deliberation._try_deliver",
                new_callable=AsyncMock,
            ),
        ):
            mock_embedder.embed_one = AsyncMock(
                return_value=np.array([1.0, 0.0, 0.0], dtype=np.float32)
            )

            from theo.conversation.deliberation import _run_deliberation

            await _run_deliberation(delib_id, session_id, "test")

        # All 5 phases ran despite monitor failures.
        assert mock_update.await_count == 5
        mock_complete.assert_awaited_once()

    async def test_disabled_metacognition_skips_monitor(self) -> None:
        """When metacognition_enabled is False, monitor is never called."""
        from uuid import UUID

        session_id = UUID("00000000-0000-0000-0000-000000000001")
        delib_id = UUID("00000000-0000-0000-0000-000000000099")

        settings = _make_settings(metacognition_enabled=False)
        settings.deliberation_phase_timeout_s = 120
        settings.deliberation_budget_tokens = 20_000

        async def mock_stream(messages, **kwargs):  # noqa: ARG001
            return _stream_result()

        with (
            patch(
                "theo.conversation.deliberation.get_settings",
                return_value=settings,
            ),
            patch(
                "theo.conversation.deliberation.stream_and_collect",
                side_effect=mock_stream,
            ),
            patch(
                "theo.conversation.deliberation.get_deliberation",
                new_callable=AsyncMock,
                return_value=self._make_state(),
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
                "theo.conversation.deliberation.monitor",
                new_callable=AsyncMock,
            ) as mock_monitor,
            patch(
                "theo.conversation.deliberation.embedder",
            ) as mock_embedder,
            patch(
                "theo.conversation.deliberation._try_deliver",
                new_callable=AsyncMock,
            ),
        ):
            mock_embedder.embed_one = AsyncMock(
                return_value=np.array([1.0, 0.0, 0.0], dtype=np.float32)
            )

            from theo.conversation.deliberation import _run_deliberation

            await _run_deliberation(delib_id, session_id, "test")

        # Monitor should never be called.
        mock_monitor.assert_not_awaited()
        # But embedder.embed_one should also not be called.
        mock_embedder.embed_one.assert_not_awaited()
