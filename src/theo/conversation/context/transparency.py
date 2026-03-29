"""Speed-tier reasoning transparency instructions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace

if TYPE_CHECKING:
    from theo.llm import Speed
    from theo.memory._types import DimensionResult

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

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
