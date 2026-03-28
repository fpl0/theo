"""Anthropic tool-use schemas for the memory subsystem.

These static JSON definitions are passed to the Anthropic API as the
``tools`` parameter.  They live in their own module to keep
``tools.py`` focused on dispatch and handler logic.
"""

from __future__ import annotations

from typing import Any

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "store_memory",
        "description": (
            "Store a new observation, fact, or piece of knowledge in long-term memory. "
            "Use this when the user shares something worth remembering."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": (
                        "Category of the memory (e.g. 'fact', 'preference', 'person', 'event')."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": "The content to remember.",
                },
                "importance": {
                    "type": "number",
                    "description": (
                        "How important this memory is, from 0.0 (trivial) "
                        "to 1.0 (critical). Default 0.5."
                    ),
                },
            },
            "required": ["kind", "body"],
        },
    },
    {
        "name": "search_memory",
        "description": (
            "Search long-term memory for relevant knowledge using hybrid retrieval. "
            "Combines vector similarity, full-text search, and graph traversal "
            "via Reciprocal Rank Fusion for comprehensive results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return. Default 5.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_core_memory",
        "description": (
            "Read the current state of core memory. Core memory contains four "
            "always-loaded documents: persona, goals, user_model, and context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "link_memories",
        "description": (
            "Create an explicit relationship between two memories in the knowledge graph. "
            "Use this when you notice two memories are related (e.g. a person and their "
            "project, an event and its location)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_id": {
                    "type": "integer",
                    "description": "ID of the first memory (from search results).",
                },
                "target_id": {
                    "type": "integer",
                    "description": "ID of the second memory.",
                },
                "label": {
                    "type": "string",
                    "description": (
                        "Relationship type (e.g. 'works_on', 'located_in', "
                        "'related_to', 'part_of')."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Why these memories are related.",
                },
            },
            "required": ["source_id", "target_id", "label"],
        },
    },
    {
        "name": "update_core_memory",
        "description": (
            "Update one of the four core memory documents (persona, goals, "
            "user_model, context). Use this to evolve your self-model or "
            "understanding of the user over time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "enum": ["persona", "goals", "user_model", "context"],
                    "description": "Which core memory document to update.",
                },
                "body": {
                    "type": "object",
                    "description": "The new document content (replaces existing body).",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief explanation of why this update is being made.",
                },
            },
            "required": ["label", "body"],
        },
    },
    {
        "name": "update_user_model",
        "description": (
            "Update a specific dimension of the structured user model. "
            "Use this when you learn something about the user's values, "
            "personality, communication preferences, energy patterns, "
            "goals, or boundaries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "framework": {
                    "type": "string",
                    "enum": [
                        "schwartz",
                        "big_five",
                        "narrative",
                        "communication",
                        "energy",
                        "goals",
                        "boundaries",
                    ],
                    "description": "Which framework the dimension belongs to.",
                },
                "dimension": {
                    "type": "string",
                    "description": (
                        "The specific dimension to update "
                        "(e.g. 'openness', 'verbosity', 'active_goals', 'identity_themes')."
                    ),
                },
                "value": {
                    "type": "object",
                    "description": "The updated value for this dimension.",
                },
                "reason": {
                    "type": "string",
                    "description": "What evidence supports this update.",
                },
            },
            "required": ["framework", "dimension", "value"],
        },
    },
    {
        "name": "advance_onboarding",
        "description": (
            "Mark the current onboarding phase as complete and move to the next. "
            "Call this when you feel you have enough information for the current phase."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what was learned in this phase.",
                },
            },
            "required": ["summary"],
        },
    },
]
