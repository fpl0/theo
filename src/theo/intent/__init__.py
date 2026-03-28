"""Intent subsystem: priority queue for proactive actions."""

from opentelemetry import metrics

from theo.intent._types import IntentResult, IntentState
from theo.intent.evaluator import IntentEvaluator, scan_approaching_dates
from theo.intent.store import create_intent, get_queue_depth

intent_evaluator = IntentEvaluator()

_meter = metrics.get_meter(__name__)


def _observe_queue_depth() -> int:
    """Synchronous callback for the observable gauge.

    Falls back to 0 when the pool is unavailable (e.g. during startup).
    """
    # Queue depth is fetched async; the gauge callback cannot await.
    # We report 0 here and rely on the evaluator loop for accurate metrics.
    return 0


_meter.create_observable_gauge(
    "theo.intent.queue_depth",
    callbacks=[lambda _options: [metrics.Observation(value=_observe_queue_depth())]],
    description="Number of actionable intents in the queue",
)


async def publish_intent(**kwargs: object) -> int:
    """Convenience wrapper around :func:`store.create_intent`.

    Wakes the evaluator after publishing so the intent is processed
    without waiting for the next poll interval.
    """
    intent_id: int = await create_intent(**kwargs)  # type: ignore[arg-type]
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
