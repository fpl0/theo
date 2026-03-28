# Reasoning Transparency (FPL-35)

**Date:** 2026-03-28

## Context

Theo's responses lacked structure — the same formatting was used regardless of whether a message required deep analysis or a quick acknowledgment. Users had no visibility into Theo's reasoning process, and communication style didn't adapt to user preferences.

## Decision: Speed-tier-specific response guidelines

Each speed tier gets distinct formatting instructions injected into the system prompt as a `## Response Guidelines` section:

- **Deliberative** — structured 4-part response: recommendation first, then reasoning chain, confidence level, and alternatives considered. This makes the thinking process transparent for complex questions.
- **Reflective** — direct answer first, with relevant context from memory when it adds value. No rigid structure.
- **Reactive** — brief, matching the energy of the message. No over-explaining greetings or acknowledgments.

The instructions are placed immediately after the Persona section and before Goals, so they inform response style without interfering with identity or task context.

## Decision: User model verbosity adaptation

The deliberative and reflective tiers have concise variants selected when the user's `communication/verbosity` dimension is set to `"concise"`. The reactive tier is already minimal so it has no variant. When no verbosity preference exists (dimension not found or not yet seeded), the default (detailed) variant is used.

This reads a single dimension via `get_dimension("communication", "verbosity")` during context assembly — one lightweight DB query gathered in parallel with `hybrid_search` to avoid adding serial latency.

## Decision: Transparency instructions always present

The transparency section is always included in the system prompt (even with empty core memory). This ensures consistent response formatting from the first interaction, before onboarding seeds any preferences.

## Decision: Telemetry refactoring

Adding the transparency section pushed `assemble()` past the 50-statement lint limit. The telemetry recording was extracted into `_record_telemetry()` with a `_ContextTelemetry` dataclass that consolidates all token counts. This keeps the main function focused on assembly logic.

## Files changed

- `src/theo/conversation/context/formatting.py` — added `build_transparency_instructions()`, `_resolve_verbosity()`, and per-tier instruction constants
- `src/theo/conversation/context/assembly.py` — added `speed` parameter to `assemble()`, verbosity dimension fetch, transparency section wiring, extracted `_record_telemetry()` with `_ContextTelemetry`
- `src/theo/conversation/context/__init__.py` — re-exported `build_transparency_instructions`
- `src/theo/conversation/turn.py` — passes `speed` to `assemble()`
- `tests/test_context.py` — 13 new tests covering all speed tiers, verbosity variants, malformed input, and section ordering
