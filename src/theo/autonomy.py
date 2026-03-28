"""Autonomy classification — trust layer between initiative and control.

Every action Theo takes gets classified by autonomy level, determining
how much owner involvement is required:

- **autonomous**: execute immediately, no notification
- **inform**: execute, then notify owner
- **propose**: present plan and wait for approval
- **consult**: ask owner for input before formulating a plan

Classification considers: action type defaults, owner overrides from
core memory, and action context.  Every classified action is logged
to ``action_log`` for audit and future autonomy graduation (M5).
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import TYPE_CHECKING, Any, Literal

from opentelemetry import metrics, trace

from theo.db import db

if TYPE_CHECKING:
    from uuid import UUID

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

_decisions_counter = _meter.create_counter(
    "theo.autonomy.decisions",
    description="Total autonomy decisions by level and action type",
)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

type AutonomyLevel = Literal["autonomous", "inform", "propose", "consult"]
type ActionDecision = Literal["executed", "approved", "rejected", "modified", "timed_out"]

# Action types and their default autonomy levels.
# The ticket defines these mappings explicitly.
type ActionType = Literal[
    "memory_store",
    "memory_search",
    "core_memory_update",
    "contradiction_resolve",
    "deliberation_start",
    "plan_create",
    "plan_execute_step",
    "external_action",
]

_DEFAULT_LEVELS: dict[str, AutonomyLevel] = {
    "memory_store": "autonomous",
    "memory_search": "autonomous",
    "core_memory_update": "inform",
    "contradiction_resolve": "inform",
    "deliberation_start": "autonomous",
    "plan_create": "propose",
    "plan_execute_step": "propose",
    "external_action": "propose",
}

# Map tool names to action types for turn-level classification.
_TOOL_ACTION_MAP: dict[str, ActionType] = {
    "store_memory": "memory_store",
    "search_memory": "memory_search",
    "read_core_memory": "memory_search",
    "update_core_memory": "core_memory_update",
    "link_memories": "memory_store",
    "update_user_model": "core_memory_update",
    "advance_onboarding": "core_memory_update",
}


@dataclasses.dataclass(frozen=True, slots=True)
class Classification:
    """Immutable result of an autonomy classification."""

    action_type: str
    autonomy_level: AutonomyLevel
    reason: str


@dataclasses.dataclass(frozen=True, slots=True)
class ActionLogEntry:
    """Immutable snapshot of an action_log row."""

    id: int
    action_type: str
    autonomy_level: AutonomyLevel
    decision: ActionDecision
    context: dict[str, Any]
    session_id: UUID | None
    intent_id: int | None


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_INSERT_LOG = """
INSERT INTO action_log (action_type, autonomy_level, decision, context, session_id, intent_id)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING id, action_type, autonomy_level, decision, context, session_id, intent_id
"""

_COUNT_CONSECUTIVE_EXECUTED = """
SELECT count(*) AS streak
FROM (
    SELECT decision
    FROM action_log
    WHERE action_type = $1
    ORDER BY created_at DESC
    LIMIT $2
) AS recent
WHERE decision = 'executed'
"""


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(
    action_type: str,
    *,
    owner_overrides: dict[str, AutonomyLevel] | None = None,
) -> Classification:
    """Classify an action type to its autonomy level.

    Classification considers (in priority order):
    1. Owner overrides from core memory
    2. Default level from the action type registry
    3. Fallback to ``propose`` for unknown action types
    """
    with tracer.start_as_current_span(
        "autonomy.classify",
        attributes={"autonomy.action_type": action_type},
    ):
        # 1. Check owner overrides
        if owner_overrides and action_type in owner_overrides:
            level = owner_overrides[action_type]
            reason = "owner override"
            log.info(
                "autonomy classified via owner override",
                extra={"action_type": action_type, "level": level},
            )
        # 2. Check default registry
        elif action_type in _DEFAULT_LEVELS:
            level = _DEFAULT_LEVELS[action_type]
            reason = "default registry"
        # 3. Unknown action types default to propose (safe)
        else:
            level = "propose"
            reason = "unknown action type, defaulting to propose"
            log.warning(
                "unknown action type, defaulting to propose",
                extra={"action_type": action_type},
            )

        _decisions_counter.add(
            1,
            {"autonomy.level": level, "autonomy.action_type": action_type},
        )

        return Classification(
            action_type=action_type,
            autonomy_level=level,
            reason=reason,
        )


def classify_tool(
    tool_name: str,
    *,
    owner_overrides: dict[str, AutonomyLevel] | None = None,
) -> Classification:
    """Classify a tool call by mapping tool name to action type.

    Tools not in the mapping are treated as ``external_action`` (propose).
    """
    action_type = _TOOL_ACTION_MAP.get(tool_name, "external_action")
    return classify(action_type, owner_overrides=owner_overrides)


# ---------------------------------------------------------------------------
# Action logging
# ---------------------------------------------------------------------------


async def log_action(  # noqa: PLR0913
    action_type: str,
    autonomy_level: AutonomyLevel,
    decision: ActionDecision,
    *,
    context: dict[str, object] | None = None,
    session_id: UUID | None = None,
    intent_id: int | None = None,
) -> ActionLogEntry:
    """Record an autonomy-classified action in the action log."""
    with tracer.start_as_current_span(
        "autonomy.log_action",
        attributes={
            "autonomy.action_type": action_type,
            "autonomy.level": autonomy_level,
            "autonomy.decision": decision,
        },
    ):
        ctx_json = json.dumps(context) if context else "{}"
        row = await db.pool.fetchrow(
            _INSERT_LOG,
            action_type,
            autonomy_level,
            decision,
            ctx_json,
            session_id,
            intent_id,
        )
        entry = _row_to_entry(row)
        log.info(
            "action logged",
            extra={
                "action_log_id": entry.id,
                "action_type": action_type,
                "autonomy_level": autonomy_level,
                "decision": decision,
            },
        )
        return entry


async def count_consecutive_executed(action_type: str, *, window: int = 10) -> int:
    """Count consecutive successful executions for an action type.

    Looks at the most recent *window* actions of the given type and
    counts how many consecutive ones were ``executed``.  This is the
    foundation for M5 autonomy graduation.
    """
    with tracer.start_as_current_span(
        "autonomy.count_consecutive",
        attributes={"autonomy.action_type": action_type},
    ):
        count = await db.pool.fetchval(_COUNT_CONSECUTIVE_EXECUTED, action_type, window)
        return int(count) if count else 0


def requires_approval(level: AutonomyLevel) -> bool:
    """Return True if this autonomy level requires owner approval."""
    return level in {"propose", "consult"}


def action_type_for_tool(tool_name: str) -> str:
    """Map a tool name to its action type, defaulting to ``external_action``."""
    return _TOOL_ACTION_MAP.get(tool_name, "external_action")


# ---------------------------------------------------------------------------
# Owner overrides
# ---------------------------------------------------------------------------


def parse_owner_overrides(core_context: dict[str, object] | None) -> dict[str, AutonomyLevel]:
    """Extract autonomy overrides from core_memory context document.

    Overrides are stored as a JSON object under the ``autonomy_overrides``
    key in the core memory ``context`` document.  Example::

        {"autonomy_overrides": {"core_memory_update": "propose"}}
    """
    if not core_context:
        return {}
    raw = core_context.get("autonomy_overrides")
    if not isinstance(raw, dict):
        return {}
    valid_levels: dict[str, AutonomyLevel] = {
        "autonomous": "autonomous",
        "inform": "inform",
        "propose": "propose",
        "consult": "consult",
    }
    result: dict[str, AutonomyLevel] = {}
    for k, v in raw.items():
        if isinstance(v, str) and v in valid_levels:
            result[str(k)] = valid_levels[v]
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_entry(row: Any) -> ActionLogEntry:
    ctx = row["context"]
    if isinstance(ctx, str):
        ctx = json.loads(ctx)
    return ActionLogEntry(
        id=row["id"],
        action_type=row["action_type"],
        autonomy_level=row["autonomy_level"],
        decision=row["decision"],
        context=ctx if isinstance(ctx, dict) else {},
        session_id=row["session_id"],
        intent_id=row["intent_id"],
    )
