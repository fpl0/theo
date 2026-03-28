"""Token estimation and truncation utilities."""

from __future__ import annotations

from opentelemetry import trace

tracer = trace.get_tracer(__name__)

_TOKENS_PER_WORD: float = 1.3


def estimate_tokens(text: str) -> int:
    """Rough token count from word count (~1.3 tokens per word).

    Intentionally coarse; a tokenizer-backed implementation can replace
    this later without changing the public API.
    """
    if not text:
        return 0
    return max(1, int(len(text.split()) * _TOKENS_PER_WORD))


def truncate_section(text: str, *, budget: int) -> str:
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
