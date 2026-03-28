"""Result types for the intent subsystem."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from datetime import datetime

type IntentState = Literal[
    "proposed", "approved", "executing", "completed", "failed", "expired", "cancelled"
]


@dataclasses.dataclass(frozen=True, slots=True)
class IntentResult:
    """An intent retrieved from the queue."""

    id: int
    type: str
    state: IntentState
    base_priority: int
    source_module: str
    payload: dict[str, Any]
    deadline: datetime | None
    budget_tokens: int | None
    attempts: int
    max_attempts: int
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    expires_at: datetime | None
    effective_priority: float | None = None
