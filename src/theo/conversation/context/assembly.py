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
from theo.memory import core
from theo.memory.episodes import list_episodes
from theo.memory.retrieval import hybrid_search
from theo.onboarding.prompts import get_phase_system_prompt

if TYPE_CHECKING:
    from uuid import UUID

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
) -> AssembledContext:
    """Assemble the full context window for a conversation turn.

    Parameters
    ----------
    session_id:
        The current session, used to fetch recent episodes.
    latest_message:
        Text of the newest user message, used for archival retrieval.

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

        relevant_nodes = await hybrid_search(latest_message, limit=20)
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

        system_prompt = join_system_prompt(
            sections,
            memory_section,
            onboarding_section=onboarding_section,
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
        total_tokens = (
            section_tokens.persona
            + section_tokens.goals
            + section_tokens.user_model
            + section_tokens.current_task
            + section_tokens.memory
            + section_tokens.history
            + onboarding_tokens
        )

        span.set_attribute("context.persona_tokens", section_tokens.persona)
        span.set_attribute("context.goals_tokens", section_tokens.goals)
        span.set_attribute("context.user_model_tokens", section_tokens.user_model)
        span.set_attribute("context.task_tokens", section_tokens.current_task)
        span.set_attribute("context.memory_tokens", section_tokens.memory)
        span.set_attribute("context.history_tokens", section_tokens.history)
        span.set_attribute("context.onboarding_tokens", onboarding_tokens)
        span.set_attribute("context.total_tokens", total_tokens)
        span.set_attribute("context.message_count", len(messages))

        log.info(
            "assembled context",
            extra={
                "session_id": str(session_id),
                "persona_tokens": section_tokens.persona,
                "goals_tokens": section_tokens.goals,
                "user_model_tokens": section_tokens.user_model,
                "task_tokens": section_tokens.current_task,
                "memory_tokens": section_tokens.memory,
                "history_tokens": section_tokens.history,
                "onboarding_tokens": onboarding_tokens,
                "total_tokens": total_tokens,
                "message_count": len(messages),
            },
        )

        _duration.record(time.monotonic() - t0)

        return AssembledContext(
            system_prompt=system_prompt,
            messages=messages,
            token_estimate=total_tokens,
            section_tokens=section_tokens,
        )
