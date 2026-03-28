"""Intent subsystem: priority queue for proactive actions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from theo.intent._types import IntentResult, IntentState
from theo.intent.evaluator import IntentEvaluator, scan_approaching_dates
from theo.intent.store import create_intent, get_queue_depth

if TYPE_CHECKING:
    from datetime import datetime

log = logging.getLogger(__name__)

intent_evaluator = IntentEvaluator()


async def publish_intent(  # noqa: PLR0913
    *,
    intent_type: str,
    source_module: str,
    base_priority: int = 50,
    payload: dict[str, Any] | None = None,
    deadline: datetime | None = None,
    budget_tokens: int | None = None,
    max_attempts: int = 3,
    expires_at: datetime | None = None,
    state: IntentState = "proposed",
) -> int:
    """Create an intent and wake the evaluator for immediate processing."""
    intent_id = await create_intent(
        intent_type=intent_type,
        source_module=source_module,
        base_priority=base_priority,
        payload=payload,
        deadline=deadline,
        budget_tokens=budget_tokens,
        max_attempts=max_attempts,
        expires_at=expires_at,
        state=state,
    )
    intent_evaluator.wake()
    return intent_id


__all__ = [
    "IntentEvaluator",
    "IntentResult",
    "IntentState",
    "create_intent",
    "get_queue_depth",
    "intent_evaluator",
    "publish_intent",
    "scan_approaching_dates",
]
