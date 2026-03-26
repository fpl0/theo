"""Context assembly for each conversation turn.

Builds the context window from Theo's three memory tiers:

- **Core memory** — always present, never truncated (persona, goals, user
  model, current context).
- **Archival memory** — relevant knowledge graph nodes retrieved via vector
  similarity against the latest message.
- **Recall memory** — recent episodes from the current session, converted to
  Anthropic's alternating user/assistant message format.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from typing import TYPE_CHECKING

from opentelemetry import metrics, trace

from theo.memory import core
from theo.memory.episodes import list_episodes
from theo.memory.nodes import search_nodes

if TYPE_CHECKING:
    from uuid import UUID

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

    Intentionally coarse for M1; a tokenizer-backed implementation can replace
    this later without changing the public API.
    """
    if not text:
        return 0
    return max(1, int(len(text.split()) * _TOKENS_PER_WORD))


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class AssembledContext:
    """The assembled context window ready for the LLM."""

    system_prompt: str
    messages: list[dict[str, str]]
    token_estimate: int


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

# Ordered labels for deterministic system prompt layout.
_CORE_LABELS: tuple[CoreMemoryLabel, ...] = ("persona", "goals", "user_model", "context")

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


def _format_core_memory(docs: dict[CoreMemoryLabel, CoreDocument]) -> str:
    """Build the core memory portion of the system prompt."""
    sections: list[str] = []
    for label in _CORE_LABELS:
        doc = docs.get(label)
        if doc is not None:
            sections.append(_format_core_section(doc))
    return "\n\n".join(sections)


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
    # Map episode roles → Anthropic roles. Tool and system episodes are
    # presented as user context since Anthropic only accepts user/assistant.
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

    # Enforce budget — drop from the front (oldest) first.
    total = sum(estimate_tokens(m["content"]) for m in merged)
    while merged and total > budget:
        total -= estimate_tokens(merged[0]["content"])
        merged.pop(0)

    # Anthropic requires the first message to be from the user.
    if merged and merged[0]["role"] == "assistant":
        total -= estimate_tokens(merged[0]["content"])
        merged.pop(0)

    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def assemble(
    *,
    session_id: UUID,
    latest_message: str,
    memory_budget: int = 2000,
    history_budget: int = 4000,
) -> AssembledContext:
    """Assemble the full context window for a conversation turn.

    Parameters
    ----------
    session_id:
        The current session, used to fetch recent episodes.
    latest_message:
        Text of the newest user message, used for archival retrieval.
    memory_budget:
        Approximate token cap for the *Relevant Memories* section.
    history_budget:
        Approximate token cap for the message history.

    Returns
    -------
    AssembledContext
        System prompt, messages, and a rough token estimate for the full
        context window.
    """
    with tracer.start_as_current_span(
        "assemble_context",
        attributes={"session.id": str(session_id)},
    ) as span:
        t0 = time.monotonic()

        # 1. Core memory — always included, never truncated
        core_docs = await core.read_all()
        core_section = _format_core_memory(core_docs)
        core_tokens = estimate_tokens(core_section)

        # 2. Archival retrieval — vector search against latest message
        relevant_nodes = await search_nodes(latest_message, limit=20)
        memory_section = _format_relevant_memories(relevant_nodes, budget=memory_budget)
        memory_tokens = estimate_tokens(memory_section) if memory_section else 0

        # 3. Assemble system prompt
        parts = [core_section] if core_section else []
        if memory_section:
            parts.append(memory_section)
        system_prompt = "\n\n".join(parts)

        # 4. Recall — recent session episodes as messages
        eps = await list_episodes(session_id)
        messages = _episodes_to_messages(eps, budget=history_budget)
        history_tokens = sum(estimate_tokens(m["content"]) for m in messages)

        total_tokens = core_tokens + memory_tokens + history_tokens

        # Record per-section token counts on the span
        span.set_attribute("context.core_tokens", core_tokens)
        span.set_attribute("context.memory_tokens", memory_tokens)
        span.set_attribute("context.history_tokens", history_tokens)
        span.set_attribute("context.total_tokens", total_tokens)
        span.set_attribute("context.message_count", len(messages))

        log.info(
            "assembled context",
            extra={
                "session_id": str(session_id),
                "core_tokens": core_tokens,
                "memory_tokens": memory_tokens,
                "history_tokens": history_tokens,
                "total_tokens": total_tokens,
                "message_count": len(messages),
            },
        )

        _duration.record(time.monotonic() - t0)

        return AssembledContext(
            system_prompt=system_prompt,
            messages=messages,
            token_estimate=total_tokens,
        )
