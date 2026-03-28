"""Context assembly for each conversation turn.

Builds the context window from Theo's three memory tiers:

- **Core memory** — always present; persona and goals are never truncated,
  user model and current context have guaranteed minimums.
- **Archival memory** — relevant knowledge graph nodes retrieved via hybrid
  search (vector + FTS + graph fusion).
- **Recall memory** — recent episodes from the current session, converted to
  Anthropic's alternating user/assistant message format.

Eviction policy when total exceeds capacity:

1. Trim retrieved memories first.
2. Trim history second (drop oldest messages).
3. User model and current context trimmed only as last resort.
4. Persona and goals are **never** truncated.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import TYPE_CHECKING

from opentelemetry import metrics, trace

from theo.config import get_settings
from theo.conversation.context.formatting import (
    apply_eviction,
    build_core_sections,
    episodes_to_messages,
    extract_onboarding_state,
    format_relevant_memories,
    join_system_prompt,
)
from theo.conversation.context.tokens import estimate_tokens
from theo.conversation.context.transparency import build_transparency_instructions
from theo.conversation.deliberation import deliver_pending
from theo.memory import core
from theo.memory.episodes import list_episodes
from theo.memory.retrieval import hybrid_search
from theo.memory.user_model import get_dimension
from theo.onboarding.prompts import get_phase_system_prompt

if TYPE_CHECKING:
    from uuid import UUID

    from theo.llm import Speed

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

_duration = _meter.create_histogram(
    "theo.context.duration",
    unit="s",
    description="Context assembly duration",
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class SectionTokens:
    """Per-section token counts for observability."""

    persona: int
    goals: int
    user_model: int
    current_task: int
    memory: int
    history: int


@dataclasses.dataclass(frozen=True, slots=True)
class AssembledContext:
    """The assembled context window ready for the LLM."""

    system_prompt: str
    messages: list[dict[str, str]]
    token_estimate: int
    section_tokens: SectionTokens


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def assemble(
    *,
    session_id: UUID,
    latest_message: str,
    speed: Speed = "reflective",
) -> AssembledContext:
    """Assemble the full context window for a conversation turn.

    Parameters
    ----------
    session_id:
        The current session, used to fetch recent episodes.
    latest_message:
        Text of the newest user message, used for archival retrieval.
    speed:
        Classified speed tier for this turn, used to select reasoning
        transparency instructions.

    Returns
    -------
    AssembledContext
        System prompt, messages, per-section token counts, and a rough
        token estimate for the full context window.
    """
    cfg = get_settings()

    with tracer.start_as_current_span(
        "assemble_context",
        attributes={"session.id": str(session_id)},
    ) as span:
        t0 = time.monotonic()

        core_docs = await core.read_all()
        sections = build_core_sections(core_docs)
        apply_eviction(sections, cfg)

        relevant_nodes, verbosity_dim = await asyncio.gather(
            hybrid_search(latest_message, limit=20),
            get_dimension("communication", "verbosity"),
        )
        memory_section = format_relevant_memories(
            relevant_nodes,
            budget=cfg.context_memory_budget,
        )
        memory_tokens = estimate_tokens(memory_section) if memory_section else 0

        # Check for active onboarding from already-fetched core docs
        onboarding_section = ""
        onboarding_tokens = 0
        onboarding_state = extract_onboarding_state(core_docs.get("context"))

        if onboarding_state is not None:
            try:
                phase_prompt = get_phase_system_prompt(onboarding_state.phase)
            except KeyError:
                log.warning(
                    "unknown onboarding phase, skipping prompt injection",
                    extra={"phase": onboarding_state.phase},
                )
            else:
                phase_num = onboarding_state.phase_index + 1
                phase_name = onboarding_state.phase.replace("_", " ").title()
                onboarding_section = (
                    f"## Onboarding (Phase {phase_num}: {phase_name})\n{phase_prompt}"
                )
                onboarding_tokens = estimate_tokens(onboarding_section)
                span.set_attribute("context.onboarding_phase", onboarding_state.phase)

        # Build reasoning transparency instructions for the current speed tier.
        transparency_section = build_transparency_instructions(speed, verbosity_dim)
        transparency_tokens = estimate_tokens(transparency_section)
        span.set_attribute("context.speed_tier", speed)

        # Check for completed deliberations awaiting delivery.
        deliberation_section = ""
        deliberation_tokens = 0
        pending_results = await deliver_pending(session_id)
        if pending_results:
            deliberation_section = (
                "## Completed Deliberation Results\n"
                "You previously deliberated on a question in the background. "
                "Here are the results — incorporate them into your response.\n\n"
                + "\n\n---\n\n".join(pending_results)
            )
            deliberation_tokens = estimate_tokens(deliberation_section)
            span.set_attribute("context.deliberation_results", len(pending_results))

        system_prompt = join_system_prompt(
            sections,
            memory_section,
            onboarding_section=onboarding_section,
            transparency_section=transparency_section,
            deliberation_section=deliberation_section,
        )

        eps = await list_episodes(session_id)
        messages = episodes_to_messages(eps, budget=cfg.context_history_budget)
        history_tokens = sum(estimate_tokens(m["content"]) for m in messages)

        section_tokens = SectionTokens(
            persona=sections.persona_tokens,
            goals=sections.goals_tokens,
            user_model=sections.user_model_tokens,
            current_task=sections.task_tokens,
            memory=memory_tokens,
            history=history_tokens,
        )
        telemetry = _ContextTelemetry(
            sections=section_tokens,
            onboarding=onboarding_tokens,
            transparency=transparency_tokens,
            deliberation=deliberation_tokens,
            message_count=len(messages),
        )
        _record_telemetry(span, session_id, telemetry)

        _duration.record(time.monotonic() - t0)

        return AssembledContext(
            system_prompt=system_prompt,
            messages=messages,
            token_estimate=telemetry.total,
            section_tokens=section_tokens,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class _ContextTelemetry:
    """All token counts needed for observability."""

    sections: SectionTokens
    onboarding: int = 0
    transparency: int = 0
    deliberation: int = 0
    message_count: int = 0

    @property
    def total(self) -> int:
        s = self.sections
        return (
            s.persona
            + s.goals
            + s.user_model
            + s.current_task
            + s.memory
            + s.history
            + self.onboarding
            + self.transparency
            + self.deliberation
        )


def _record_telemetry(
    span: trace.Span,
    session_id: UUID,
    t: _ContextTelemetry,
) -> None:
    """Write per-section token counts to the active span and structured log."""
    s = t.sections
    span.set_attribute("context.persona_tokens", s.persona)
    span.set_attribute("context.goals_tokens", s.goals)
    span.set_attribute("context.user_model_tokens", s.user_model)
    span.set_attribute("context.task_tokens", s.current_task)
    span.set_attribute("context.memory_tokens", s.memory)
    span.set_attribute("context.history_tokens", s.history)
    span.set_attribute("context.onboarding_tokens", t.onboarding)
    span.set_attribute("context.transparency_tokens", t.transparency)
    span.set_attribute("context.deliberation_tokens", t.deliberation)
    span.set_attribute("context.total_tokens", t.total)
    span.set_attribute("context.message_count", t.message_count)

    log.info(
        "assembled context",
        extra={
            "session_id": str(session_id),
            "persona_tokens": s.persona,
            "goals_tokens": s.goals,
            "user_model_tokens": s.user_model,
            "task_tokens": s.current_task,
            "memory_tokens": s.memory,
            "history_tokens": s.history,
            "onboarding_tokens": t.onboarding,
            "transparency_tokens": t.transparency,
            "deliberation_tokens": t.deliberation,
            "total_tokens": t.total,
            "message_count": t.message_count,
        },
    )
