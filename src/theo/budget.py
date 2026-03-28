"""Token budget tracking and enforcement."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from opentelemetry import metrics, trace

from theo.bus import bus
from theo.bus.events import BudgetWarning
from theo.config import get_settings
from theo.db import db
from theo.errors import BudgetExceededError

if TYPE_CHECKING:
    from uuid import UUID

    from theo.llm import Speed

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

_tokens_used = _meter.create_counter(
    "theo.budget.tokens_used",
    description="Total tokens consumed",
)
_cost_counter = _meter.create_counter(
    "theo.budget.cost",
    description="Estimated cost of token usage",
)
_cap_rejections = _meter.create_counter(
    "theo.budget.cap_rejections",
    description="LLM calls rejected due to budget cap",
)

type UsageSource = Literal["conversation", "deliberation", "intent"]

# Track last-warned 5% band per scope to avoid flooding.
_warned_bands: dict[str, int] = {}

# ── SQL ──────────────────────────────────────────────────────────────

_INSERT_USAGE = """
    INSERT INTO token_usage (session_id, model, input_tokens, output_tokens,
                             estimated_cost, source)
    VALUES ($1, $2, $3, $4, $5, $6)
"""

_DAILY_TOTAL = """
    SELECT coalesce(sum(input_tokens + output_tokens), 0)::bigint
    FROM token_usage
    WHERE created_at > now() - interval '1 day'
"""

_SESSION_TOTAL = """
    SELECT coalesce(sum(input_tokens + output_tokens), 0)::bigint
    FROM token_usage
    WHERE session_id = $1
"""


# ── Usage record ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Immutable record of token usage from a single LLM call."""

    session_id: UUID
    model: str
    input_tokens: int
    output_tokens: int
    speed: Speed
    source: UsageSource = "conversation"


# ── Cost estimation ──────────────────────────────────────────────────


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    speed: Speed,
) -> float:
    """Estimate cost for a given token count and speed tier."""
    cfg = get_settings()
    rate_map: dict[str, float] = {
        "reactive": cfg.budget_cost_reactive_per_1k,
        "reflective": cfg.budget_cost_reflective_per_1k,
        "deliberative": cfg.budget_cost_deliberative_per_1k,
    }
    rate = rate_map.get(speed, cfg.budget_cost_reflective_per_1k)
    total_tokens = input_tokens + output_tokens
    return total_tokens * rate / 1000


# ── Recording ────────────────────────────────────────────────────────


async def record_usage(record: UsageRecord) -> None:
    """Record token usage and emit metrics. Check warning thresholds."""
    with tracer.start_as_current_span(
        "budget.record_usage",
        attributes={
            "session.id": str(record.session_id),
            "budget.model": record.model,
            "budget.source": record.source,
            "budget.input_tokens": record.input_tokens,
            "budget.output_tokens": record.output_tokens,
        },
    ):
        cost = estimate_cost(record.input_tokens, record.output_tokens, record.speed)

        await db.pool.execute(
            _INSERT_USAGE,
            record.session_id,
            record.model,
            record.input_tokens,
            record.output_tokens,
            cost,
            record.source,
        )

        total = record.input_tokens + record.output_tokens
        _tokens_used.add(total, {"model": record.model, "source": record.source})
        _cost_counter.add(cost, {"model": record.model, "source": record.source})

        log.info(
            "token usage recorded",
            extra={
                "session_id": str(record.session_id),
                "model": record.model,
                "source": record.source,
                "input_tokens": record.input_tokens,
                "output_tokens": record.output_tokens,
                "estimated_cost": round(cost, 4),
            },
        )

        await _check_warnings(record.session_id)


# ── Budget checking ──────────────────────────────────────────────────


async def check_budget(session_id: UUID) -> None:
    """Check daily and session budgets. Raise BudgetExceededError if exceeded."""
    with tracer.start_as_current_span(
        "budget.check",
        attributes={"session.id": str(session_id)},
    ):
        cfg = get_settings()

        daily_used = await get_daily_total()
        if daily_used >= cfg.budget_daily_cap_tokens:
            _cap_rejections.add(1, {"scope": "daily"})
            log.warning(
                "daily budget exceeded",
                extra={
                    "daily_used": daily_used,
                    "daily_cap": cfg.budget_daily_cap_tokens,
                },
            )
            msg = f"Daily token budget exceeded ({daily_used:,}/{cfg.budget_daily_cap_tokens:,})"
            raise BudgetExceededError(msg)

        session_used = await get_session_total(session_id)
        if session_used >= cfg.budget_session_cap_tokens:
            _cap_rejections.add(1, {"scope": "session"})
            log.warning(
                "session budget exceeded",
                extra={
                    "session_id": str(session_id),
                    "session_used": session_used,
                    "session_cap": cfg.budget_session_cap_tokens,
                },
            )
            msg = (
                f"Session token budget exceeded "
                f"({session_used:,}/{cfg.budget_session_cap_tokens:,})"
            )
            raise BudgetExceededError(msg)


# ── Aggregate queries ────────────────────────────────────────────────


async def get_daily_total() -> int:
    """Return total tokens used in the last 24 hours."""
    with tracer.start_as_current_span("budget.get_daily_total"):
        row = await db.pool.fetchval(_DAILY_TOTAL)
        return int(row)


async def get_session_total(session_id: UUID) -> int:
    """Return total tokens used in a specific session."""
    with tracer.start_as_current_span(
        "budget.get_session_total",
        attributes={"session.id": str(session_id)},
    ):
        row = await db.pool.fetchval(_SESSION_TOTAL, session_id)
        return int(row)


# ── Warning checks ───────────────────────────────────────────────────

_BAND_SIZE = 5  # Emit warnings at 5% intervals (80%, 85%, 90%, 95%, 100%)


async def _check_warnings(session_id: UUID) -> None:
    """Emit BudgetWarning events when usage crosses a new 5% band."""
    cfg = get_settings()

    daily_used = await get_daily_total()
    daily_ratio = daily_used / cfg.budget_daily_cap_tokens
    if daily_ratio >= cfg.budget_warning_threshold:
        band = int(daily_ratio * 100) // _BAND_SIZE
        if band > _warned_bands.get("daily", 0):
            _warned_bands["daily"] = band
            await bus.publish(
                BudgetWarning(
                    session_id=session_id,
                    scope="daily",
                    used_tokens=daily_used,
                    cap_tokens=cfg.budget_daily_cap_tokens,
                    usage_ratio=round(daily_ratio, 3),
                ),
            )

    session_used = await get_session_total(session_id)
    session_ratio = session_used / cfg.budget_session_cap_tokens
    if session_ratio >= cfg.budget_warning_threshold:
        key = f"session:{session_id}"
        band = int(session_ratio * 100) // _BAND_SIZE
        if band > _warned_bands.get(key, 0):
            _warned_bands[key] = band
            await bus.publish(
                BudgetWarning(
                    session_id=session_id,
                    scope="session",
                    used_tokens=session_used,
                    cap_tokens=cfg.budget_session_cap_tokens,
                    usage_ratio=round(session_ratio, 3),
                ),
            )
