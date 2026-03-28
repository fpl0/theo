"""Formatting helpers for context window sections."""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import TYPE_CHECKING

from opentelemetry import trace

from theo.conversation.context.tokens import estimate_tokens, truncate_section
from theo.onboarding.flow import dict_to_state

if TYPE_CHECKING:
    from theo.config import Settings
    from theo.llm import Speed
    from theo.memory._types import DimensionResult, EpisodeResult, NodeResult
    from theo.memory.core import CoreDocument, CoreMemoryLabel
    from theo.onboarding.flow import OnboardingState

tracer = trace.get_tracer(__name__)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core memory formatting
# ---------------------------------------------------------------------------

_SECTION_TITLES: dict[str, str] = {
    "persona": "Persona",
    "goals": "Goals",
    "user_model": "User Model",
    "context": "Current Context",
}


def format_core_section(doc: CoreDocument) -> str:
    """Format a single core memory document as a markdown section."""
    title = _SECTION_TITLES[doc.label]
    body = json.dumps(doc.body, indent=2, ensure_ascii=False)
    return f"## {title}\n{body}"


# ---------------------------------------------------------------------------
# Archival memory formatting
# ---------------------------------------------------------------------------


def format_relevant_memories(results: list[NodeResult], *, budget: int) -> str:
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


# ---------------------------------------------------------------------------
# Episode / history formatting
# ---------------------------------------------------------------------------


def episodes_to_messages(
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
# Core section assembly
# ---------------------------------------------------------------------------


@dataclasses.dataclass(slots=True)  # mutable: apply_eviction mutates fields in place
class CoreSections:
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


def build_core_sections(docs: dict[CoreMemoryLabel, CoreDocument]) -> CoreSections:
    """Format each core memory document into its section text."""
    sections = CoreSections()
    if "persona" in docs:
        sections.persona = format_core_section(docs["persona"])
    if "goals" in docs:
        sections.goals = format_core_section(docs["goals"])
    if "user_model" in docs:
        sections.user_model = format_core_section(docs["user_model"])
    if "context" in docs:
        sections.task = format_core_section(docs["context"])
    return sections


def apply_eviction(sections: CoreSections, cfg: Settings) -> None:
    """Enforce per-section caps on trimmable core sections.

    Persona and goals are **never** truncated.  User model and current
    task are capped at their configured budgets when they exceed them.
    """
    if sections.user_model_tokens > cfg.context_user_model_budget:
        sections.user_model = truncate_section(
            sections.user_model,
            budget=cfg.context_user_model_budget,
        )
    if sections.task_tokens > cfg.context_current_task_budget:
        sections.task = truncate_section(
            sections.task,
            budget=cfg.context_current_task_budget,
        )


def join_system_prompt(
    sections: CoreSections,
    memory_section: str,
    *,
    onboarding_section: str = "",
    transparency_section: str = "",
    deliberation_section: str = "",
) -> str:
    """Join non-empty sections in canonical order."""
    parts: list[str] = []
    if onboarding_section:
        parts.append(onboarding_section)
    if sections.persona:
        parts.append(sections.persona)
    if transparency_section:
        parts.append(transparency_section)
    if sections.goals:
        parts.append(sections.goals)
    if sections.user_model:
        parts.append(sections.user_model)
    if sections.task:
        parts.append(sections.task)
    if memory_section:
        parts.append(memory_section)
    if deliberation_section:
        parts.append(deliberation_section)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Onboarding state extraction
# ---------------------------------------------------------------------------


def extract_onboarding_state(context_doc: CoreDocument | None) -> OnboardingState | None:
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
# Reasoning transparency
# ---------------------------------------------------------------------------

type Verbosity = str | None

_DELIBERATIVE_INSTRUCTIONS = """\
## Response Guidelines
Structure your response as follows:
1. **Recommendation** — lead with a clear, actionable answer.
2. **Reasoning** — explain the key factors behind your conclusion.
3. **Confidence** — state your confidence level and what it's based on.
4. **Alternatives** — mention what else you considered and why it's less preferred."""

_DELIBERATIVE_CONCISE_INSTRUCTIONS = """\
## Response Guidelines
Structure your response as follows:
1. **Recommendation** — lead with a clear, actionable answer.
2. **Reasoning** — briefly state the key factors (keep it tight).
3. **Confidence** — one sentence on confidence level.
4. **Alternatives** — mention alternatives only when meaningfully different."""

_REFLECTIVE_INSTRUCTIONS = """\
## Response Guidelines
Be direct — answer first, then share relevant context from memory when it adds value."""

_REFLECTIVE_CONCISE_INSTRUCTIONS = """\
## Response Guidelines
Be direct and concise. Answer first. Add context only when essential."""

_REACTIVE_INSTRUCTIONS = """\
## Response Guidelines
Be brief. Match the energy of the message — no over-explaining."""


def _resolve_verbosity(dimension: DimensionResult | None) -> Verbosity:
    """Extract the verbosity preference string from a dimension result."""
    if dimension is None:
        return None
    raw = dimension.value.get("verbosity")
    if isinstance(raw, str):
        return raw
    return None


def build_transparency_instructions(
    speed: Speed,
    verbosity_dimension: DimensionResult | None = None,
) -> str:
    """Build speed-tier-specific reasoning transparency instructions.

    When a user model *verbosity* dimension is available and its value is
    ``"concise"``, the deliberative and reflective tiers use shorter variants
    that still preserve structure but reduce detail.
    """
    verbosity = _resolve_verbosity(verbosity_dimension)
    is_concise = verbosity == "concise"

    if speed == "deliberative":
        return _DELIBERATIVE_CONCISE_INSTRUCTIONS if is_concise else _DELIBERATIVE_INSTRUCTIONS
    if speed == "reflective":
        return _REFLECTIVE_CONCISE_INSTRUCTIONS if is_concise else _REFLECTIVE_INSTRUCTIONS
    # reactive
    return _REACTIVE_INSTRUCTIONS
