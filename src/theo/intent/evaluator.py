"""Intent evaluator — background loop that processes the intent queue.

Mirrors the retry queue pattern: runs as a named asyncio task, wakes on
signal or interval, and processes one intent at a time.

Three-tier throttle based on conversation state:
- **Foreground active** (engine.inflight > 0): defer LLM-requiring intents
- **Foreground idle** (inflight == 0): run normally
- **Foreground absent** (>15 min since last message): reduced interval
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from opentelemetry import metrics, trace

from theo.config import get_settings
from theo.errors import IntentBudgetExhaustedError, IntentExpiredError
from theo.intent import store

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

type ThrottleTier = Literal["active", "idle", "absent"]
type IntentHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

_eval_counter = meter.create_counter(
    "theo.intent.evaluated",
    description="Intents evaluated by the evaluator",
)
_eval_duration = meter.create_histogram(
    "theo.intent.eval_duration",
    unit="s",
    description="Duration of a single intent evaluation",
)
_expired_counter = meter.create_counter(
    "theo.intent.expired",
    description="Intents expired by the evaluator",
)

_ABSENT_THRESHOLD_S = 900  # 15 minutes
_STOP_TIMEOUT_S = 30

# Date patterns for the approaching-dates scanner.
_DATE_PATTERN = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})\b"  # ISO dates like 2026-04-15
    r"|\b(\d{1,2}/\d{1,2}/\d{4})\b",  # US dates like 4/15/2026
)


class IntentEvaluator:
    """Background evaluator that drains the intent queue."""

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()
        self._handlers: dict[str, IntentHandler] = {}
        self._last_message_time: float = time.monotonic()
        # Set by the conversation engine when inflight changes.
        self._engine_inflight: int = 0

    # ── handler registration ─────────────────────────────────────────

    def register_handler(self, intent_type: str, handler: IntentHandler) -> None:
        """Register a handler for a specific intent type."""
        self._handlers[intent_type] = handler
        log.info("registered intent handler", extra={"intent_type": intent_type})

    # ── throttle ─────────────────────────────────────────────────────

    @property
    def throttle_tier(self) -> ThrottleTier:
        """Current throttle tier based on conversation state."""
        if self._engine_inflight > 0:
            return "active"
        elapsed = time.monotonic() - self._last_message_time
        if elapsed > _ABSENT_THRESHOLD_S:
            return "absent"
        return "idle"

    def notify_message(self) -> None:
        """Called when a user message is received to reset absent timer."""
        self._last_message_time = time.monotonic()

    def update_inflight(self, count: int) -> None:
        """Called by the conversation engine to update inflight count."""
        self._engine_inflight = count

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the evaluator background loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._eval_loop(), name="intent-evaluator")
        log.info("intent evaluator started")

    async def stop(self) -> None:
        """Stop the evaluator and wait for the current task to finish."""
        if not self._running:
            return
        self._running = False
        self._wakeup.set()
        if self._task is not None:
            try:
                async with asyncio.timeout(_STOP_TIMEOUT_S):
                    await self._task
            except TimeoutError:
                log.warning("intent evaluator stop timed out, cancelling")
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None
        log.info("intent evaluator stopped")

    def wake(self) -> None:
        """Signal the evaluator to check the queue immediately."""
        self._wakeup.set()

    # ── main loop ─────────────────────────────────────────────────────

    async def _eval_loop(self) -> None:
        """Continuously evaluate intents from the queue."""
        cfg = get_settings()
        interval = cfg.intent_evaluator_interval_s
        # Absent tier uses a reduced interval for higher throughput.
        absent_interval = max(1, interval // 3)

        while self._running:
            try:
                # Expire overdue intents each cycle.
                expired = await store.expire_overdue()
                if expired:
                    _expired_counter.add(len(expired))

                # Check budget before attempting to fetch.
                usage = await store.get_daily_token_usage()
                if usage >= cfg.intent_max_daily_budget_tokens:
                    log.debug(
                        "daily budget exhausted, skipping cycle",
                        extra={"usage": usage, "budget": cfg.intent_max_daily_budget_tokens},
                    )
                    await self._wait(interval)
                    continue

                # Throttle: skip when foreground is active.
                tier = self.throttle_tier
                if tier == "active":
                    log.debug("foreground active, deferring intents")
                    await self._wait(interval)
                    continue

                # Atomically fetch and start the next intent.
                intent = await store.fetch_and_start()
                if intent is None:
                    wait_s = absent_interval if tier == "absent" else interval
                    await self._wait(wait_s)
                    continue

                # Process the intent. Loop back immediately after
                # to drain the queue without waiting.
                await self._evaluate_one(intent.id, intent.type, intent.payload)

            except Exception:
                log.exception("evaluator cycle error")
                await self._wait(interval)

    async def _evaluate_one(
        self, intent_id: int, intent_type: str, payload: dict[str, Any]
    ) -> None:
        """Evaluate a single intent (already in 'executing' state)."""
        t0 = time.monotonic()

        with tracer.start_as_current_span(
            "intent.evaluate",
            attributes={
                "intent.id": intent_id,
                "intent.type": intent_type,
                "intent.throttle_tier": self.throttle_tier,
            },
        ):
            handler = self._handlers.get(intent_type)
            if handler is None:
                log.warning(
                    "no handler for intent type",
                    extra={"intent_id": intent_id, "intent_type": intent_type},
                )
                await store.complete_intent(
                    intent_id,
                    state="failed",
                    error=f"no handler registered for type {intent_type!r}",
                )
                return

            try:
                result = await handler(intent_type, payload)
                await store.complete_intent(intent_id, state="completed", result=result)
                _eval_counter.add(1, {"intent.type": intent_type, "intent.outcome": "completed"})
            except IntentExpiredError as exc:
                await store.complete_intent(intent_id, state="expired", error=str(exc))
                _eval_counter.add(1, {"intent.type": intent_type, "intent.outcome": "expired"})
            except IntentBudgetExhaustedError as exc:
                await store.complete_intent(intent_id, state="failed", error=str(exc))
                _eval_counter.add(
                    1, {"intent.type": intent_type, "intent.outcome": "budget_exhausted"}
                )
            except Exception as exc:
                log.exception(
                    "intent handler failed",
                    extra={"intent_id": intent_id, "intent_type": intent_type},
                )
                await store.complete_intent(intent_id, state="failed", error=str(exc))
                _eval_counter.add(1, {"intent.type": intent_type, "intent.outcome": "failed"})

            elapsed = time.monotonic() - t0
            _eval_duration.record(elapsed, {"intent.type": intent_type})

    async def _wait(self, seconds: int) -> None:
        """Wait for the given interval or until woken."""
        self._wakeup.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wakeup.wait(), timeout=seconds)


# ---------------------------------------------------------------------------
# Built-in intent sources
# ---------------------------------------------------------------------------


async def scan_approaching_dates() -> int:
    """Scan core memory context for approaching dates and publish intents.

    Skips dates that already have an active intent to prevent duplicates.
    Returns the number of intents created.
    """
    from theo.memory import core as core_memory  # noqa: PLC0415

    with tracer.start_as_current_span("intent.scan_dates"):
        cfg = get_settings()
        horizon = timedelta(days=cfg.intent_deadline_horizon_days)
        now = datetime.now(UTC)
        cutoff = now + horizon

        try:
            doc = await core_memory.read_one("context")
        except LookupError:
            log.debug("no context document found for date scanning")
            return 0

        # Scan the stringified body for date patterns.
        text = str(doc.body)
        count = 0

        for match in _DATE_PATTERN.finditer(text):
            date_str = match.group(1) or match.group(2)
            try:
                if match.group(1):
                    parsed = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
                else:
                    parsed = datetime.strptime(date_str, "%m/%d/%Y").replace(tzinfo=UTC)
            except ValueError:
                continue

            if now < parsed <= cutoff:
                # Skip if an active intent already exists for this date.
                if await store.intent_exists("deadline_approaching", date_str):
                    continue
                await store.create_intent(
                    intent_type="deadline_approaching",
                    source_module="intent.evaluator",
                    base_priority=80,
                    payload={"date": date_str, "context_snippet": text[:200]},
                    deadline=parsed,
                    expires_at=parsed,
                )
                count += 1

        if count:
            log.info("date scan found approaching dates", extra={"count": count})
        return count
