"""Health check — point-in-time snapshot of system health."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opentelemetry import trace

from theo.errors import DatabaseNotConnectedError

if TYPE_CHECKING:
    from theo.resilience.circuit import CircuitBreaker, CircuitState
    from theo.resilience.retry import RetryQueue

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Structured health report."""

    db_connected: bool
    api_reachable: bool
    telegram_connected: bool
    circuit_state: CircuitState
    retry_queue_depth: int


async def health_check(
    *,
    circuit: CircuitBreaker,
    queue: RetryQueue,
) -> HealthStatus:
    """Return a point-in-time health snapshot."""
    with tracer.start_as_current_span("resilience.health_check"):
        db_ok = await _check_db()
        api_ok = circuit.state == "closed"
        tg_ok = _check_telegram()

        status = HealthStatus(
            db_connected=db_ok,
            api_reachable=api_ok,
            telegram_connected=tg_ok,
            circuit_state=circuit.state,
            retry_queue_depth=queue.depth,
        )

        log.info(
            "health check",
            extra={
                "db_connected": status.db_connected,
                "api_reachable": status.api_reachable,
                "telegram_connected": status.telegram_connected,
                "circuit_state": status.circuit_state,
                "retry_queue_depth": status.retry_queue_depth,
            },
        )

        return status


async def _check_db() -> bool:
    from theo.db import db  # noqa: PLC0415

    try:
        await db.pool.execute("SELECT 1")
    except DatabaseNotConnectedError:
        return False
    else:
        return True


def _check_telegram() -> bool:
    from theo.config import get_settings  # noqa: PLC0415

    cfg = get_settings()
    return cfg.telegram_bot_token is not None
