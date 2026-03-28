# Context Assembly (FPL-12)

**Date:** 2026-03-26

## Decision: Word-count token estimation for M1

Token budgets use a rough approximation of ~1.3 tokens per word. This is intentionally coarse — accurate tokenization would require the Anthropic tokenizer (or tiktoken), adding a dependency for marginal gain at this stage. The `estimate_tokens()` function has a stable API so a tokenizer-backed implementation can replace it later without changing callers.

## Decision: Settings-driven budgets

`assemble()` reads `context_memory_budget` and `context_history_budget` from `get_settings()` directly. Budget values are configured in `Settings` (`config.py`) and loaded from env vars. This keeps budget management centralized in config rather than threaded through call sites.

## Decision: Non-assistant roles mapped to "user"

Anthropic's API only accepts `user` and `assistant` roles in the messages array. Episodes with `tool` or `system` roles are mapped to `user`. Consecutive same-role messages are merged (concatenated with double newlines) to satisfy the alternating-role constraint. This is the simplest correct mapping for M1; FPL-14 (tool integration) will introduce proper tool-result content blocks.

## Decision: Core memory never truncated

Core memory sections (persona, goals, user_model, context) are always included in full regardless of token budgets. These are typically under 2K tokens and represent Theo's essential identity — truncating them would degrade response quality more than saving the token space is worth.

## Decision: Oldest-first message dropping

When the message history exceeds `history_budget`, the oldest messages are dropped first. This preserves recency — the most recent exchanges are the most relevant for conversational coherence. If the first message after trimming is from the assistant role, it is also dropped to maintain Anthropic's user-first requirement.

## Decision: Frozen `AssembledContext` dataclass

The return type is a frozen slotted dataclass, matching the pattern established by `NodeResult` and `EpisodeResult`. The `messages` field uses `list[dict[str, str]]` (Anthropic's message format) rather than a custom type, keeping the interface compatible with the Anthropic SDK without coupling to it.
