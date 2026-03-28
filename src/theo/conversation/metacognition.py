"""Metacognitive monitor for deliberative reasoning sessions.

Runs between deliberation phases to detect pathological patterns:
spinning, overconfidence, scope drift, and diminishing returns.
Returns a decision: continue, redirect, escalate, or abort.

The monitor is a pure async function — no state, no side effects beyond
telemetry.  The deliberation engine calls it after each phase completes.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import numpy as np
from opentelemetry import metrics, trace

from theo.config import get_settings
from theo.embeddings import embedder

if TYPE_CHECKING:
    from numpy.typing import NDArray

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

type MonitorAction = Literal["continue", "redirect", "escalate", "abort"]

_checks = _meter.create_counter(
    "theo.metacognition.checks",
    description="Total metacognition checks performed",
)
_interventions = _meter.create_counter(
    "theo.metacognition.interventions",
    description="Metacognition interventions by type",
)
_duration = _meter.create_histogram(
    "theo.metacognition.duration",
    unit="s",
    description="Time to run a metacognition check",
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MonitorDecision:
    """Immutable result from a metacognition check."""

    action: MonitorAction
    reasoning: str
    redirect_prompt: str | None = None


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _cosine_similarity(a: NDArray[np.float32], b: NDArray[np.float32]) -> float:
    """Cosine similarity between two L2-normalised vectors (= dot product)."""
    return float(np.dot(a, b))


def _detect_spinning(
    phase_embeddings: dict[str, NDArray[np.float32]],
    current_phase: str,
    previous_phase: str | None,
    threshold: float,
) -> float | None:
    """Return similarity score if spinning detected, else None."""
    if previous_phase is None or previous_phase not in phase_embeddings:
        return None
    if current_phase not in phase_embeddings:
        return None

    sim = _cosine_similarity(
        phase_embeddings[current_phase],
        phase_embeddings[previous_phase],
    )
    if sim > threshold:
        return sim
    return None


def _detect_drift(
    phase_embeddings: dict[str, NDArray[np.float32]],
    question_embedding: NDArray[np.float32],
    current_phase: str,
    threshold: float,
) -> float | None:
    """Return similarity score if drift detected (below threshold), else None.

    Lower similarity to the original question = more drift.
    """
    if current_phase not in phase_embeddings:
        return None

    sim = _cosine_similarity(phase_embeddings[current_phase], question_embedding)
    if sim < threshold:
        return sim
    return None


def _detect_overconfidence(
    phase_outputs: dict[str, str],
    current_phase: str,
    nodes_referenced: list[int],
    min_evidence: int,
) -> bool:
    """Return True if overconfidence detected in evaluate/synthesize phase."""
    if current_phase not in ("evaluate", "synthesize"):
        return False

    output = phase_outputs.get(current_phase, "").lower()
    high_confidence_signals = (
        "high confidence",
        "very confident",
        "strongly recommend",
        "certainly",
    )
    claims_confidence = any(signal in output for signal in high_confidence_signals)
    return claims_confidence and len(set(nodes_referenced)) < min_evidence


def _detect_diminishing_returns(
    current_phase: str,
    nodes_referenced: list[int],
    prior_nodes_referenced: list[int],
) -> bool:
    """Return True if current phase adds no new nodes over prior phases."""
    # Only relevant after gather — generate/evaluate should build on gathered info.
    if current_phase in ("frame", "gather"):
        return False
    if not prior_nodes_referenced:
        return False

    prior_set = set(prior_nodes_referenced)
    current_set = set(nodes_referenced)
    novel = current_set - prior_set
    return len(novel) == 0 and len(current_set) > 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Matches "id": 42 or "node_id": 42 in tool result JSON embedded in text.
_NODE_ID_RE = re.compile(r'"(?:id|node_id)":\s*(\d+)')

_PHASE_ORDER = ("frame", "gather", "generate", "evaluate", "synthesize")


def extract_node_ids(text: str) -> list[int]:
    """Extract memory node IDs from text containing tool result JSON."""
    return [int(m) for m in _NODE_ID_RE.findall(text)]


def _previous_phase(current: str) -> str | None:
    """Return the phase that ran before *current*, or None for frame."""
    try:
        idx = _PHASE_ORDER.index(current)
    except ValueError:
        return None
    return _PHASE_ORDER[idx - 1] if idx > 0 else None


async def monitor(
    question_embedding: NDArray[np.float32],
    phase_outputs: dict[str, str],
    current_phase: str,
    nodes_referenced: list[int],
    prior_nodes_referenced: list[int] | None = None,
) -> MonitorDecision:
    """Check the current deliberation state for pathological patterns.

    Parameters
    ----------
    question_embedding:
        Embedding of the original question (for drift detection).
    phase_outputs:
        All phase outputs accumulated so far (including current).
    current_phase:
        The phase that just completed.
    nodes_referenced:
        Memory node IDs referenced in the current phase output.
    prior_nodes_referenced:
        Memory node IDs referenced in all prior phases combined.

    Returns
    -------
    MonitorDecision
        Action to take: continue, redirect, escalate, or abort.
    """
    t0 = time.monotonic()
    cfg = get_settings()

    with tracer.start_as_current_span(
        "metacognition.monitor",
        attributes={"deliberation.phase": current_phase},
    ) as span:
        _checks.add(1, {"deliberation.phase": current_phase})

        # Embed all available phase outputs for comparison.
        texts_to_embed = {phase: text for phase, text in phase_outputs.items() if text}
        phase_embeddings: dict[str, NDArray[np.float32]] = {}
        if texts_to_embed:
            phases = list(texts_to_embed.keys())
            texts = list(texts_to_embed.values())
            vectors = await embedder.embed(texts)
            for i, phase in enumerate(phases):
                phase_embeddings[phase] = vectors[i]

        previous = _previous_phase(current_phase)

        # 1. Spinning detection
        spinning_sim = _detect_spinning(
            phase_embeddings,
            current_phase,
            previous,
            cfg.metacognition_spinning_threshold,
        )
        if spinning_sim is not None:
            decision = MonitorDecision(
                action="redirect",
                reasoning=(
                    f"Spinning detected: phase '{current_phase}' is {spinning_sim:.2f} "
                    f"similar to '{previous}' (threshold {cfg.metacognition_spinning_threshold}). "
                    f"Redirecting to break the loop."
                ),
                redirect_prompt=(
                    f"Your previous response was too similar to the prior phase. "
                    f"Take a different angle. Focus on what is NEW or DIFFERENT "
                    f"from what you already said in the {previous} phase."
                ),
            )
            _record_intervention(span, decision, time.monotonic() - t0)
            return decision

        # 2. Scope drift detection
        drift_sim = _detect_drift(
            phase_embeddings,
            question_embedding,
            current_phase,
            cfg.metacognition_drift_threshold,
        )
        if drift_sim is not None:
            decision = MonitorDecision(
                action="redirect",
                reasoning=(
                    f"Scope drift detected: phase '{current_phase}' has only {drift_sim:.2f} "
                    f"similarity to the original question "
                    f"(threshold {cfg.metacognition_drift_threshold}). Redirecting."
                ),
                redirect_prompt=(
                    "You have drifted from the original question. "
                    "Refocus your analysis on the specific question asked."
                ),
            )
            _record_intervention(span, decision, time.monotonic() - t0)
            return decision

        # 3. Overconfidence detection
        if _detect_overconfidence(
            phase_outputs,
            current_phase,
            nodes_referenced,
            cfg.metacognition_min_evidence_for_high_confidence,
        ):
            decision = MonitorDecision(
                action="escalate",
                reasoning=(
                    f"Overconfidence detected: phase '{current_phase}' claims high confidence "
                    f"but only {len(set(nodes_referenced))} distinct memory nodes referenced "
                    f"(minimum {cfg.metacognition_min_evidence_for_high_confidence} required)."
                ),
            )
            _record_intervention(span, decision, time.monotonic() - t0)
            return decision

        # 4. Diminishing returns detection
        if _detect_diminishing_returns(
            current_phase,
            nodes_referenced,
            prior_nodes_referenced or [],
        ):
            decision = MonitorDecision(
                action="abort",
                reasoning=(
                    f"Diminishing returns: phase '{current_phase}' references no novel "
                    f"memory nodes beyond what prior phases already found. "
                    f"Aborting to save resources."
                ),
            )
            _record_intervention(span, decision, time.monotonic() - t0)
            return decision

        # No pathology detected.
        elapsed = time.monotonic() - t0
        _duration.record(elapsed)
        span.set_attribute("metacognition.action", "continue")
        log.debug(
            "metacognition check passed",
            extra={"phase": current_phase, "duration_s": round(elapsed, 3)},
        )
        return MonitorDecision(action="continue", reasoning="No pathology detected.")


def _record_intervention(span: trace.Span, decision: MonitorDecision, elapsed: float) -> None:
    """Record telemetry for an intervention."""
    _duration.record(elapsed)
    _interventions.add(1, {"metacognition.action": decision.action})
    span.set_attribute("metacognition.action", decision.action)
    span.set_attribute("metacognition.reasoning", decision.reasoning)
    log.info(
        "metacognition intervention",
        extra={
            "action": decision.action,
            "reasoning": decision.reasoning,
            "duration_s": round(elapsed, 3),
        },
    )
