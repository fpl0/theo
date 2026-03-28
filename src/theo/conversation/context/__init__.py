"""Context assembly for conversation turns.

Re-exports the public API so ``from theo.conversation.context import ...``
continues to work after the module-to-package conversion.
"""

from theo.conversation.context.assembly import AssembledContext, SectionTokens, assemble
from theo.conversation.context.formatting import (
    CoreSections,
    apply_eviction,
    build_core_sections,
    build_transparency_instructions,
    episodes_to_messages,
    extract_onboarding_state,
    format_core_section,
    format_relevant_memories,
    join_system_prompt,
)
from theo.conversation.context.tokens import estimate_tokens, truncate_section

__all__ = [
    "AssembledContext",
    "CoreSections",
    "SectionTokens",
    "apply_eviction",
    "assemble",
    "build_core_sections",
    "build_transparency_instructions",
    "episodes_to_messages",
    "estimate_tokens",
    "extract_onboarding_state",
    "format_core_section",
    "format_relevant_memories",
    "join_system_prompt",
    "truncate_section",
]
