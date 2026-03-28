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
import json
import logging
import time
from typing import TYPE_CHECKING

from opentelemetry import metrics, trace

from theo.config import get_settings
from theo.memory import core
from theo.memory.episodes import list_episodes
from theo.memory.retrieval import hybrid_search
from theo.onboarding.flow import OnboardingState, dict_to_state
from theo.onboarding.prompts import get_phase_system_prompt

if TYPE_CHECKING:
    from uuid import UUID

    from theo.config import Settings
    from theo.memory._types import EpisodeResult, NodeResult
    from theo.memory.core import CoreDocument, CoreMemoryLabel

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

_duration = _meter.create_histogram(
    "theo.context.duration",
    unit="s",
    description="Context assembly duration",
)

# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

_TOKENS_PER_WORD: float = 1.3


def estimate_tokens(text: str) -> int:
    """Rough token count from word count (~1.3 tokens per word).

    Intentionally coarse; a tokenizer-backed implementation can replace
    this later without changing the public API.
    """
    if not text:
        return 0
    return max(1, int(len(text.split()) * _TOKENS_PER_WORD))


# ---------------------------------------------------------------------------
# Result type
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
# Formatting helpers
# ---------------------------------------------------------------------------

_SECTION_TITLES: dict[str, str] = {
    "persona": "Persona",
    "goals": "Goals",
    "user_model": "User Model",
    "context": "Current Context",
}


def _format_core_section(doc: CoreDocument) -> str:
    """Format a single core memory document as a markdown section."""
    title = _SECTION_TITLES[doc.label]
    body = json.dumps(doc.body, indent=2, ensure_ascii=False)
    return f"## {title}\n{body}"


def _truncate_section(text: str, *, budget: int) -> str:
    """Truncate *text* to fit within *budget* tokens.

    Keeps as many leading words as possible. Uses the same token-per-word
    ratio as :func:`estimate_tokens` so results are consistent.
    """
    if not text or budget <= 0:
        return ""
    words = text.split()
    max_words = int(budget / _TOKENS_PER_WORD)
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def _format_relevant_memories(results: list[NodeResult], *, budget: int) -> str:
    """Format node search results into a system prompt section.

    Stops adding nodes once the cumulative token estimate exceeds *budget*.
    """
    if not results:
        return ""

    lines: list[str] = []
    used = 0
    for node in results:
        line = f"- [{node.kind}] {node.body}"
        cost = estimate_tokens(line)
        if used + cost > budget:
            break
        lines.append(line)
        used += cost

    if not lines:
        return ""
    return "## Relevant Memories\n" + "\n".join(lines)


def _episodes_to_messages(
    eps: list[EpisodeResult],
    *,
    budget: int,
) -> list[dict[str, str]]:
    """Convert episodes to Anthropic message format.

    Merges consecutive same-role messages to satisfy the alternating-role
    requirement. Drops oldest messages when the total exceeds *budget*.
    """
    merged: list[dict[str, str]] = []
    for ep in eps:
        role = "assistant" if ep.role == "assistant" else "user"
        if merged and merged[-1]["role"] == role:
            merged[-1] = {
                "role": role,
                "content": merged[-1]["content"] + "\n\n" + ep.body,
            }
        else:
            merged.append({"role": role, "content": ep.body})

    total = sum(estimate_tokens(m["content"]) for m in merged)
    while merged and total > budget:
        total -= estimate_tokens(merged[0]["content"])
        merged.pop(0)

    if merged and merged[0]["role"] == "assistant":
        total -= estimate_tokens(merged[0]["content"])
        merged.pop(0)

    return merged


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_onboarding_state(context_doc: CoreDocument | None) -> OnboardingState | None:
    """Parse onboarding state from the context core document, if present."""
    if context_doc is None:
        return None
    raw = context_doc.body.get("onboarding")
    if not isinstance(raw, dict):
        return None
    try:
        return dict_to_state(raw)
    except KeyError, ValueError:
        log.warning(
            "corrupted onboarding state in core memory, skipping prompt injection",
            extra={"raw": raw},
        )
        return None


# ---------------------------------------------------------------------------
# Core section assembly
# ---------------------------------------------------------------------------


@dataclasses.dataclass(slots=True)  # mutable: _apply_eviction mutates fields in place
class _CoreSections:
    """Mutable container for per-section text and token counts."""

    persona: str = ""
    goals: str = ""
    user_model: str = ""
    task: str = ""

    @property
    def persona_tokens(self) -> int:
        return estimate_tokens(self.persona)

    @property
    def goals_tokens(self) -> int:
        return estimate_tokens(self.goals)

    @property
    def user_model_tokens(self) -> int:
        return estimate_tokens(self.user_model)

    @property
    def task_tokens(self) -> int:
        return estimate_tokens(self.task)


def _build_core_sections(docs: dict[CoreMemoryLabel, CoreDocument]) -> _CoreSections:
    """Format each core memory document into its section text."""
    sections = _CoreSections()
    if "persona" in docs:
        sections.persona = _format_core_section(docs["persona"])
    if "goals" in docs:
        sections.goals = _format_core_section(docs["goals"])
    if "user_model" in docs:
        sections.user_model = _format_core_section(docs["user_model"])
    if "context" in docs:
        sections.task = _format_core_section(docs["context"])
    return sections


def _apply_eviction(sections: _CoreSections, cfg: Settings) -> None:
    """Enforce per-section caps on trimmable core sections.

    Persona and goals are **never** truncated.  User model and current
    task are capped at their configured budgets when they exceed them.
    """
    if sections.user_model_tokens > cfg.context_user_model_budget:
        sections.user_model = _truncate_section(
            sections.user_model,
            budget=cfg.context_user_model_budget,
        )
    if sections.task_tokens > cfg.context_current_task_budget:
        sections.task = _truncate_section(
            sections.task,
            budget=cfg.context_current_task_budget,
        )


def _join_system_prompt(
    sections: _CoreSections,
    memory_section: str,
    *,
    onboarding_section: str = "",
) -> str:
    """Join non-empty sections in canonical order."""
    parts: list[str] = []
    if onboarding_section:
        parts.append(onboarding_section)
    if sections.persona:
        parts.append(sections.persona)
    if sections.goals:
        parts.append(sections.goals)
    if sections.user_model:
        parts.append(sections.user_model)
    if sections.task:
        parts.append(sections.task)
    if memory_section:
        parts.append(memory_section)
    return "\n\n".join(parts)


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
        sections = _build_core_sections(core_docs)
        _apply_eviction(sections, cfg)

        relevant_nodes = await hybrid_search(latest_message, limit=20)
        memory_section = _format_relevant_memories(
            relevant_nodes,
            budget=cfg.context_memory_budget,
        )
        memory_tokens = estimate_tokens(memory_section) if memory_section else 0

        # Check for active onboarding from already-fetched core docs
        onboarding_section = ""
        onboarding_tokens = 0
        onboarding_state = _extract_onboarding_state(core_docs.get("context"))

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

        system_prompt = _join_system_prompt(
            sections,
            memory_section,
            onboarding_section=onboarding_section,
        )

        eps = await list_episodes(session_id)
        messages = _episodes_to_messages(eps, budget=cfg.context_history_budget)
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
