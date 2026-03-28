# Anthropic LLM Client

**Date:** 2026-03-26
**Ticket:** FPL-11

## Context

Theo needs a streaming LLM client for all reasoning. The PRD specifies three reasoning speed tiers (reactive, reflective, deliberative) mapped to Claude model tiers.

## Decisions

### Client-per-call, no singleton

`AsyncAnthropic` is created inside `stream_response()` rather than as a module-level singleton. This keeps the function self-contained and test-friendly (settings can vary per call). The constructor cost is negligible relative to API latency. If connection pooling becomes important, a lazy-initialized module singleton can be added later.

### Dataclasses over Pydantic for stream events

`TextDelta`, `ToolUseRequest`, and `StreamDone` are `@dataclass(frozen=True, slots=True)` rather than Pydantic models. They're ephemeral internal types — no serialization, no validation, no persistence. Dataclasses avoid the Pydantic overhead and keep the module's dependency surface minimal.

### Own retry logic, SDK retries disabled

The Anthropic SDK has built-in retry support (`max_retries`), but we disable it (`max_retries=0`) and implement our own. This gives us explicit control over retry policy per error type:

- **Rate limit (429):** exponential backoff (2s, 4s, 8s), max 3 retries
- **Timeout:** 1 retry, then raise
- **Connection error:** immediate `APIUnavailableError`, no retry

This matches the ticket requirements exactly and keeps retry behavior visible in our codebase.

### Speed classification as a pure function

`classify_speed()` is a standalone pure function, not a method on a class. It uses simple regex matching and message length — no ML, no API calls. This makes it trivially testable and easy to extend. The heuristics are intentionally simple: greetings/acks get the cheap model, explicit reasoning keywords or long messages get the strongest model, everything else uses the balanced tier.

### ty override for discriminated unions

The Anthropic SDK uses string-based discriminated unions (`event.type == "text"`) that ty cannot narrow. We add a targeted `unresolved-attribute = "ignore"` override for `llm.py` in `pyproject.toml`, matching the existing pattern for pydantic-settings limitations in `config.py`.

## Files changed

- `src/theo/llm.py` — new module: streaming client, speed classification, stream events
- `src/theo/errors.py` — added `APIUnavailableError`
- `src/theo/config.py` — added LLM config fields (API key, model tiers, max tokens)
- `pyproject.toml` — added `anthropic` dependency, ty override for llm.py
- `tests/conftest.py` — default `THEO_ANTHROPIC_API_KEY` for test collection
- `tests/test_llm.py` — 25 tests covering classification, streaming, tool use, retries, errors
