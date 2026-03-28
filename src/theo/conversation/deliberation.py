"""Deliberative reasoning engine — multi-step background reasoning.

When Theo receives a complex question, it engages in multi-step reasoning
instead of answering in one turn.  Five hardcoded phases execute sequentially:
frame → gather → generate → evaluate → synthesize.

After ``gather``, the LLM can signal early-exit (question is simpler than
expected) which skips directly to ``synthesize``.

Background deliberation does NOT write to the session's episode history.
Only the final delivery enters the episode stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Literal

from opentelemetry import metrics, trace

from theo.bus import bus
from theo.bus.events import MessageReceived, MetacognitionAlert
from theo.config import get_settings
from theo.conversation.metacognition import MonitorDecision, extract_node_ids, monitor
from theo.conversation.stream import stream_and_collect
from theo.deliberation import (
    DeliberationPhase,
    complete_deliberation,
    create_deliberation,
    get_deliberation,
    list_pending_delivery,
    mark_delivered,
    update_phase,
)
from theo.embeddings import embedder
from theo.errors import DeliberationError
from theo.memory.tools import TOOL_DEFINITIONS

if TYPE_CHECKING:
    from uuid import UUID

    import numpy as np
    from numpy.typing import NDArray

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

_duration = _meter.create_histogram(
    "theo.deliberation.duration",
    unit="s",
    description="End-to-end deliberation duration",
)
_phase_duration = _meter.create_histogram(
    "theo.deliberation.phase_duration",
    unit="s",
    description="Per-phase deliberation duration",
)

# Background tasks must be referenced to avoid garbage collection.
_background_tasks: set[asyncio.Task[None]] = set()

# Phase progression: fixed order, no LLM-controlled branching.
_PHASE_ORDER: list[DeliberationPhase] = [
    "frame",
    "gather",
    "generate",
    "evaluate",
    "synthesize",
]

# Sentinel the LLM can include in gather output to signal early-exit.
_EARLY_EXIT_SIGNAL = "[EARLY_EXIT]"


# ---------------------------------------------------------------------------
# Phase prompts
# ---------------------------------------------------------------------------

_PHASE_PROMPTS: dict[DeliberationPhase, str] = {
    "frame": (
        "You are in the FRAME phase of deliberative reasoning.\n\n"
        "Your task: define the question precisely.\n"
        "- Restate the question in your own words\n"
        "- Identify sub-questions that need answering\n"
        "- Assess what information you need (what you know vs what you need to find)\n"
        "- Note any assumptions that should be validated\n\n"
        "Be concise but thorough. Output your analysis as structured text."
    ),
    "gather": (
        "You are in the GATHER phase of deliberative reasoning.\n\n"
        "Your task: search memory for relevant information.\n"
        "- Use search_memory to find facts, preferences, and context\n"
        "- Check for prior deliberations on related topics\n"
        "- Look for information that addresses the sub-questions from framing\n\n"
        "Use the available tools to search. Summarize what you found and what gaps remain.\n\n"
        "IMPORTANT: If after gathering information you determine the question is "
        "straightforward and does not need multi-step reasoning, include the exact "
        "text [EARLY_EXIT] in your response. This will skip directly to synthesis."
    ),
    "generate": (
        "You are in the GENERATE phase of deliberative reasoning.\n\n"
        "Your task: produce multiple candidate responses or approaches.\n"
        "- Generate at least 2-3 distinct candidates\n"
        "- Each candidate should be a complete approach, not a fragment\n"
        "- Consider different angles, trade-offs, and perspectives\n"
        "- Draw on the information gathered in previous phases\n\n"
        "Present each candidate clearly labeled (Candidate A, B, C, etc.)."
    ),
    "evaluate": (
        "You are in the EVALUATE phase of deliberative reasoning.\n\n"
        "Your task: assess each candidate against clear criteria.\n"
        "- Accuracy: Is it factually correct given what we know?\n"
        "- Relevance: Does it address the actual question?\n"
        "- User fit: Does it match the user's preferences and context?\n"
        "- Confidence: How certain are we in this approach?\n"
        "- Completeness: Does it address all sub-questions?\n\n"
        "Score or rank each candidate. Identify the strongest elements from each."
    ),
    "synthesize": (
        "You are in the SYNTHESIZE phase of deliberative reasoning.\n\n"
        "Your task: combine the best elements into a final response.\n"
        "- Lead with a clear recommendation or answer\n"
        "- Incorporate the strongest elements from evaluation\n"
        "- Note your confidence level and key reasoning\n"
        "- Mention alternatives briefly if relevant\n\n"
        "This output will be delivered to the user. Make it clear, actionable, "
        "and well-structured. Do NOT include phase labels or meta-commentary "
        "about the deliberation process."
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def start_deliberation(session_id: UUID, question: str) -> UUID:
    """Create a deliberation row and spawn the background task.

    Returns the deliberation UUID immediately.  The background task
    executes phases sequentially and delivers the result when done.
    """
    state = await create_deliberation(session_id, question)
    deliberation_id = state.deliberation_id

    task = asyncio.create_task(
        _safe_run(deliberation_id, session_id, question),
        name=f"deliberation-{deliberation_id}",
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    log.info(
        "spawned deliberation task",
        extra={
            "deliberation_id": str(deliberation_id),
            "session_id": str(session_id),
        },
    )
    return deliberation_id


async def deliver_pending(session_id: UUID) -> list[str]:
    """Check for completed deliberations and return their results.

    Used by context assembly to inject pending results into the next turn.
    Returns a list of formatted result strings (one per deliberation).
    """
    with tracer.start_as_current_span(
        "deliver_pending_deliberations",
        attributes={"session.id": str(session_id)},
    ):
        pending = await list_pending_delivery(session_id)
        results: list[str] = []
        for delib in pending:
            synthesis = delib.phase_outputs.get("synthesize", "")
            if synthesis:
                results.append(synthesis)
                await mark_delivered(delib.deliberation_id)
                log.info(
                    "delivered deliberation result",
                    extra={"deliberation_id": str(delib.deliberation_id)},
                )
        return results


# ---------------------------------------------------------------------------
# Background execution
# ---------------------------------------------------------------------------


async def _safe_run(
    deliberation_id: UUID,
    session_id: UUID,
    question: str,
) -> None:
    """Run deliberation phases, catching all errors."""
    try:
        await _run_deliberation(deliberation_id, session_id, question)
    except Exception:
        log.exception(
            "deliberation failed",
            extra={"deliberation_id": str(deliberation_id)},
        )
        with contextlib.suppress(LookupError):
            await complete_deliberation(deliberation_id, status="failed")


async def _run_deliberation(
    deliberation_id: UUID,
    session_id: UUID,
    question: str,
) -> None:
    """Execute the full phase progression for a deliberation."""
    cfg = get_settings()
    t0 = time.monotonic()

    with tracer.start_as_current_span(
        "deliberation.run",
        attributes={
            "deliberation.id": str(deliberation_id),
            "session.id": str(session_id),
        },
    ) as span:
        phase_outputs: dict[str, str] = {}
        phases_to_run = list(_PHASE_ORDER)

        # Precompute question embedding for metacognition drift detection.
        question_embedding: NDArray[np.float32] | None = None
        if cfg.metacognition_enabled:
            question_embedding = await embedder.embed_one(question)

        # Track all node IDs referenced across phases for diminishing returns.
        all_nodes_referenced: list[int] = []

        for phase in phases_to_run:
            # Check deliberation is still running (may have been cancelled).
            current = await get_deliberation(deliberation_id)
            if current is None or current.status != "running":
                log.info(
                    "deliberation no longer running, stopping",
                    extra={
                        "deliberation_id": str(deliberation_id),
                        "status": current.status if current else "not_found",
                    },
                )
                return

            output = await _run_phase(
                deliberation_id,
                question,
                phase,
                phase_outputs,
                timeout_s=cfg.deliberation_phase_timeout_s,
            )
            phase_outputs[phase] = output

            # Early exit: after gather, if LLM signals the question is simple.
            if phase == "gather" and _EARLY_EXIT_SIGNAL in output:
                log.info(
                    "early exit after gather",
                    extra={"deliberation_id": str(deliberation_id)},
                )
                span.set_attribute("deliberation.early_exit", "true")
                # Skip generate+evaluate, jump to synthesize.
                synth_output = await _run_phase(
                    deliberation_id,
                    question,
                    "synthesize",
                    phase_outputs,
                    timeout_s=cfg.deliberation_phase_timeout_s,
                )
                phase_outputs["synthesize"] = synth_output
                break

            # Metacognition: check for pathological patterns after each phase.
            if cfg.metacognition_enabled and question_embedding is not None:
                decision = await _check_metacognition(
                    deliberation_id,
                    question_embedding,
                    phase_outputs,
                    phase,
                    all_nodes_referenced,
                )
                if decision.action == "abort":
                    span.set_attribute("deliberation.aborted_by_metacognition", "true")
                    await complete_deliberation(deliberation_id)
                    elapsed = time.monotonic() - t0
                    _duration.record(elapsed)
                    # Deliver best-effort answer with abort notice.
                    await _try_deliver(deliberation_id, session_id, phase_outputs)
                    return
                if decision.action == "escalate":
                    span.set_attribute("deliberation.escalated", "true")
                    await _publish_alert(
                        deliberation_id,
                        session_id,
                        "escalate",
                        decision.reasoning,
                    )
                    # Continue deliberation — escalation is advisory.
                if decision.action == "redirect" and decision.redirect_prompt:
                    span.set_attribute("deliberation.redirected", "true")
                    # Prepend the redirect instruction to the next phase's output
                    # by storing it so _build_phase_system picks it up.
                    phase_outputs[f"_redirect_{phase}"] = decision.redirect_prompt

            # Update cumulative node references.
            all_nodes_referenced.extend(extract_node_ids(output))

        await complete_deliberation(deliberation_id)
        elapsed = time.monotonic() - t0
        _duration.record(elapsed)
        span.set_attribute("deliberation.duration_s", round(elapsed, 3))
        span.set_attribute("deliberation.phases_completed", len(phase_outputs))

        log.info(
            "deliberation completed",
            extra={
                "deliberation_id": str(deliberation_id),
                "duration_s": round(elapsed, 3),
                "phases": list(phase_outputs.keys()),
            },
        )

        # Attempt immediate delivery via internal message.
        await _try_deliver(deliberation_id, session_id, phase_outputs)


async def _run_phase(
    deliberation_id: UUID,
    question: str,
    phase: DeliberationPhase,
    prior_outputs: dict[str, str],
    *,
    timeout_s: int,
) -> str:
    """Execute a single deliberation phase."""
    t0 = time.monotonic()

    with tracer.start_as_current_span(
        f"deliberation.phase.{phase}",
        attributes={
            "deliberation.id": str(deliberation_id),
            "deliberation.phase": phase,
        },
    ) as span:
        system = _build_phase_system(phase, question, prior_outputs)
        messages: list[dict[str, object]] = [
            {"role": "user", "content": question},
        ]

        # Only the gather phase gets memory tools. Exclude start_deliberation
        # to prevent recursive deliberation spawning.
        tools = (
            [t for t in TOOL_DEFINITIONS if t["name"] != "start_deliberation"]
            if phase == "gather"
            else None
        )

        try:
            result = await asyncio.wait_for(
                stream_and_collect(
                    messages,
                    system=system,
                    speed="deliberative",
                    tools=tools,
                ),
                timeout=timeout_s,
            )
        except TimeoutError as exc:
            msg = f"phase {phase} timed out after {timeout_s}s"
            raise DeliberationError(msg) from exc

        output = result.text
        await update_phase(
            deliberation_id,
            _next_phase(phase),
            output,
            output_key=phase,
        )

        elapsed = time.monotonic() - t0
        _phase_duration.record(elapsed, {"deliberation.phase": phase})
        span.set_attribute("deliberation.phase_duration_s", round(elapsed, 3))
        span.set_attribute("deliberation.phase_tokens_in", result.input_tokens)
        span.set_attribute("deliberation.phase_tokens_out", result.output_tokens)

        log.info(
            "phase completed",
            extra={
                "deliberation_id": str(deliberation_id),
                "phase": phase,
                "duration_s": round(elapsed, 3),
                "output_length": len(output),
            },
        )
        return output


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_phase_system(
    phase: DeliberationPhase,
    question: str,
    prior_outputs: dict[str, str],
) -> str:
    """Build the system prompt for a deliberation phase."""
    parts = [_PHASE_PROMPTS[phase]]

    parts.append(f"\n\n## Original question\n{question}")

    if prior_outputs:
        parts.append("\n\n## Prior phase outputs")
        for p, output in prior_outputs.items():
            parts.append(f"\n### {p.title()}\n{output}")

    return "".join(parts)


# ---------------------------------------------------------------------------
# Phase progression
# ---------------------------------------------------------------------------

# Map each phase to what gets stored as the *new* phase in the DB row.
# The DB phase field tracks where the deliberation IS, not where it's been.
# After running "frame", we advance to "gather", etc.
_NEXT_PHASE: dict[DeliberationPhase, DeliberationPhase] = {
    "frame": "gather",
    "gather": "generate",
    "generate": "evaluate",
    "evaluate": "synthesize",
    "synthesize": "complete",
}


def _next_phase(current: DeliberationPhase) -> DeliberationPhase:
    return _NEXT_PHASE[current]


# ---------------------------------------------------------------------------
# Metacognition integration
# ---------------------------------------------------------------------------


async def _check_metacognition(
    deliberation_id: UUID,
    question_embedding: NDArray[np.float32],
    phase_outputs: dict[str, str],
    current_phase: str,
    all_prior_nodes: list[int],
) -> MonitorDecision:
    """Run the metacognition monitor, returning continue on failure."""
    current_output = phase_outputs.get(current_phase, "")
    current_nodes = extract_node_ids(current_output)

    try:
        return await monitor(
            question_embedding=question_embedding,
            phase_outputs=phase_outputs,
            current_phase=current_phase,
            nodes_referenced=current_nodes,
            prior_nodes_referenced=all_prior_nodes,
        )
    except Exception:  # noqa: BLE001
        log.warning(
            "metacognition check failed, continuing",
            extra={"deliberation_id": str(deliberation_id)},
            exc_info=True,
        )
        return MonitorDecision(
            action="continue", reasoning="Monitor error — defaulting to continue."
        )


async def _publish_alert(
    deliberation_id: UUID,
    session_id: UUID,
    action: Literal["redirect", "escalate", "abort"],
    reasoning: str,
) -> None:
    """Publish a MetacognitionAlert event for escalation."""
    try:
        await bus.publish(
            MetacognitionAlert(
                session_id=session_id,
                deliberation_id=deliberation_id,
                action=action,
                reasoning=reasoning,
            )
        )
    except Exception:  # noqa: BLE001
        log.warning(
            "failed to publish metacognition alert",
            extra={"deliberation_id": str(deliberation_id)},
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


async def _try_deliver(
    deliberation_id: UUID,
    session_id: UUID,
    phase_outputs: dict[str, str],
) -> None:
    """Attempt to deliver the result by publishing an internal message.

    If the session is still active, the engine will pick up the internal
    message and formulate a response incorporating the deliberation result.
    If not, delivery stays pending for the next user message.
    """
    synthesis = phase_outputs.get("synthesize", "")
    if not synthesis:
        return

    try:
        # Mark delivered first to prevent double-delivery if deliver_pending
        # runs concurrently (it also calls mark_delivered with NOT delivered guard).
        await mark_delivered(deliberation_id)
    except LookupError:
        # Already delivered by deliver_pending — skip the bus publish.
        log.info(
            "deliberation already delivered by deferred path",
            extra={"deliberation_id": str(deliberation_id)},
        )
        return
    except Exception:  # noqa: BLE001
        # mark_delivered failure is not fatal — stays pending for next turn.
        log.warning(
            "failed to mark deliberation delivered",
            extra={"deliberation_id": str(deliberation_id)},
            exc_info=True,
        )
        return

    try:
        await bus.publish(
            MessageReceived(
                body=f"[Deliberation complete] {synthesis}",
                session_id=session_id,
                channel="internal",
                role="system",
            )
        )
        log.info(
            "delivered deliberation via internal message",
            extra={"deliberation_id": str(deliberation_id)},
        )
    except Exception:  # noqa: BLE001
        log.warning(
            "failed to publish deliberation message",
            extra={"deliberation_id": str(deliberation_id)},
            exc_info=True,
        )
