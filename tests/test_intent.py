"""Tests for the intent queue and evaluator."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from theo.errors import IntentBudgetExhaustedError, IntentExpiredError
from theo.intent._types import IntentResult
from theo.intent.evaluator import _ABSENT_THRESHOLD_S, IntentEvaluator, scan_approaching_dates
from theo.intent.store import (
    complete_intent,
    create_intent,
    expire_overdue,
    fetch_and_start,
    get_daily_token_usage,
)

# ── Fixtures ──────────────────────────────────────────────────────────


def _make_intent(**overrides: object) -> IntentResult:
    """Create a minimal IntentResult for testing."""
    defaults: dict[str, object] = {
        "id": 1,
        "type": "test_intent",
        "state": "proposed",
        "base_priority": 50,
        "source_module": "tests",
        "payload": {},
        "deadline": None,
        "budget_tokens": None,
        "attempts": 0,
        "max_attempts": 3,
        "result": None,
        "error": None,
        "created_at": datetime.now(UTC),
        "updated_at": None,
        "started_at": None,
        "completed_at": None,
        "expires_at": None,
        "effective_priority": 50.0,
    }
    defaults.update(overrides)
    return IntentResult(**defaults)


def _mock_pool() -> MagicMock:
    """Create a mock asyncpg pool."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetch = AsyncMock(return_value=[])
    pool.execute = AsyncMock()
    return pool


@pytest.fixture
def mock_db():
    """Patch the db singleton to use a mock pool."""
    pool = _mock_pool()
    with patch("theo.intent.store.db") as mock:
        mock.pool = pool
        yield pool


# ── IntentResult tests ────────────────────────────────────────────────


class TestIntentResult:
    def test_frozen(self) -> None:
        intent = _make_intent()
        with pytest.raises(AttributeError):
            intent.state = "completed"  # type: ignore[misc]

    def test_fields(self) -> None:
        intent = _make_intent(type="contradiction_detected", base_priority=70)
        assert intent.type == "contradiction_detected"
        assert intent.base_priority == 70


# ── Store tests ───────────────────────────────────────────────────────


class TestIntentStore:
    async def test_create_intent(self, mock_db: MagicMock) -> None:
        result = await create_intent(
            intent_type="test",
            source_module="tests",
            base_priority=50,
            payload={"key": "value"},
        )
        assert result == 1
        mock_db.fetchval.assert_called_once()
        call_args = mock_db.fetchval.call_args
        assert call_args[0][1] == "test"  # type
        assert call_args[0][2] == "proposed"  # state
        assert call_args[0][3] == 50  # base_priority
        assert json.loads(call_args[0][5]) == {"key": "value"}  # payload

    async def test_fetch_and_start_empty(self, mock_db: MagicMock) -> None:
        # acquire() returns an async context manager yielding a mock connection.
        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_conn.transaction = MagicMock(return_value=MagicMock())
        mock_conn.transaction.return_value.__aenter__ = AsyncMock(return_value=None)
        mock_conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_db.acquire = MagicMock(return_value=MagicMock())
        mock_db.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_db.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await fetch_and_start()
        assert result is None

    async def test_expire_overdue(self, mock_db: MagicMock) -> None:
        mock_db.fetch = AsyncMock(return_value=[{"id": 10}, {"id": 20}])
        expired = await expire_overdue()
        assert expired == [10, 20]

    async def test_get_daily_token_usage(self, mock_db: MagicMock) -> None:
        mock_db.fetchval = AsyncMock(return_value=12345)
        usage = await get_daily_token_usage()
        assert usage == 12345

    async def test_complete_intent_success(self, mock_db: MagicMock) -> None:
        await complete_intent(42, state="completed", result={"tokens_used": 100})
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args[0]
        assert call_args[1] == 42
        assert call_args[2] == "completed"

    async def test_complete_intent_failed(self, mock_db: MagicMock) -> None:
        await complete_intent(42, state="failed", error="boom")
        call_args = mock_db.execute.call_args[0]
        assert call_args[2] == "failed"
        assert call_args[4] == "boom"


# ── Evaluator throttle tests ─────────────────────────────────────────


class TestEvaluatorThrottle:
    def test_active_when_inflight(self) -> None:
        ev = IntentEvaluator()
        ev.update_inflight(1)
        assert ev.throttle_tier == "active"

    def test_idle_when_no_inflight(self) -> None:
        ev = IntentEvaluator()
        ev.update_inflight(0)
        ev.notify_message()
        assert ev.throttle_tier == "idle"

    def test_absent_after_threshold(self) -> None:
        ev = IntentEvaluator()
        ev.update_inflight(0)
        # Set last message time to be past the threshold.
        ev._last_message_time = time.monotonic() - _ABSENT_THRESHOLD_S - 1
        assert ev.throttle_tier == "absent"

    def test_message_resets_to_idle(self) -> None:
        ev = IntentEvaluator()
        ev._last_message_time = time.monotonic() - _ABSENT_THRESHOLD_S - 1
        assert ev.throttle_tier == "absent"
        ev.notify_message()
        assert ev.throttle_tier == "idle"


# ── Evaluator lifecycle tests ────────────────────────────────────────


class TestEvaluatorLifecycle:
    async def test_start_stop(self) -> None:
        ev = IntentEvaluator()
        with (
            patch("theo.intent.evaluator.store") as mock_store,
            patch("theo.intent.evaluator.get_settings") as mock_settings,
        ):
            cfg = MagicMock()
            cfg.intent_evaluator_interval_s = 1
            cfg.intent_max_daily_budget_tokens = 50000
            mock_settings.return_value = cfg
            mock_store.expire_overdue = AsyncMock(return_value=[])
            mock_store.get_daily_token_usage = AsyncMock(return_value=0)
            mock_store.fetch_and_start = AsyncMock(return_value=None)

            await ev.start()
            assert ev._running
            assert ev._task is not None

            await ev.stop()
            assert not ev._running
            assert ev._task is None

    async def test_start_is_idempotent(self) -> None:
        ev = IntentEvaluator()
        with (
            patch("theo.intent.evaluator.store") as mock_store,
            patch("theo.intent.evaluator.get_settings") as mock_settings,
        ):
            cfg = MagicMock()
            cfg.intent_evaluator_interval_s = 1
            cfg.intent_max_daily_budget_tokens = 50000
            mock_settings.return_value = cfg
            mock_store.expire_overdue = AsyncMock(return_value=[])
            mock_store.get_daily_token_usage = AsyncMock(return_value=0)
            mock_store.fetch_and_start = AsyncMock(return_value=None)

            await ev.start()
            first_task = ev._task
            await ev.start()
            assert ev._task is first_task
            await ev.stop()

    async def test_stop_is_idempotent(self) -> None:
        ev = IntentEvaluator()
        await ev.stop()  # should not raise

    async def test_wake_triggers_check(self) -> None:
        ev = IntentEvaluator()
        fetch_count = 0

        async def counting_fetch() -> None:
            nonlocal fetch_count
            fetch_count += 1

        with (
            patch("theo.intent.evaluator.store") as mock_store,
            patch("theo.intent.evaluator.get_settings") as mock_settings,
        ):
            cfg = MagicMock()
            cfg.intent_evaluator_interval_s = 60  # long interval
            cfg.intent_max_daily_budget_tokens = 50000
            mock_settings.return_value = cfg
            mock_store.expire_overdue = AsyncMock(return_value=[])
            mock_store.get_daily_token_usage = AsyncMock(return_value=0)
            mock_store.fetch_and_start = AsyncMock(side_effect=counting_fetch)

            await ev.start()
            # Give the loop a moment to start.
            await asyncio.sleep(0.05)
            initial = fetch_count
            ev.wake()
            await asyncio.sleep(0.1)
            assert fetch_count > initial
            await ev.stop()


# ── Evaluator intent processing tests ─────────────────────────────────


class TestEvaluatorProcessing:
    async def test_handler_success(self) -> None:
        ev = IntentEvaluator()
        results: list[dict[str, object]] = []

        async def handler(_intent_type: str, payload: dict[str, object]) -> dict[str, object]:
            results.append(payload)
            return {"tokens_used": 100}

        ev.register_handler("test_intent", handler)

        with patch("theo.intent.evaluator.store") as mock_store:
            mock_store.complete_intent = AsyncMock()
            await ev._evaluate_one(1, "test_intent", {"key": "value"})

        assert len(results) == 1
        assert results[0] == {"key": "value"}
        mock_store.complete_intent.assert_called_once_with(
            1, state="completed", result={"tokens_used": 100}
        )

    async def test_handler_not_found(self) -> None:
        ev = IntentEvaluator()

        with patch("theo.intent.evaluator.store") as mock_store:
            mock_store.complete_intent = AsyncMock()
            await ev._evaluate_one(1, "unknown_type", {})

        mock_store.complete_intent.assert_called_once()
        call_kwargs = mock_store.complete_intent.call_args
        assert call_kwargs[1]["state"] == "failed"
        assert "no handler" in call_kwargs[1]["error"]

    async def test_handler_expired_error(self) -> None:
        ev = IntentEvaluator()

        async def handler(_type: str, _payload: dict[str, object]) -> dict[str, object]:
            msg = "past deadline"
            raise IntentExpiredError(msg)

        ev.register_handler("test_intent", handler)

        with patch("theo.intent.evaluator.store") as mock_store:
            mock_store.complete_intent = AsyncMock()
            await ev._evaluate_one(1, "test_intent", {})

        call_kwargs = mock_store.complete_intent.call_args
        assert call_kwargs[1]["state"] == "expired"

    async def test_handler_budget_exhausted_error(self) -> None:
        ev = IntentEvaluator()

        async def handler(_type: str, _payload: dict[str, object]) -> dict[str, object]:
            msg = "over budget"
            raise IntentBudgetExhaustedError(msg)

        ev.register_handler("test_intent", handler)

        with patch("theo.intent.evaluator.store") as mock_store:
            mock_store.complete_intent = AsyncMock()
            await ev._evaluate_one(1, "test_intent", {})

        call_kwargs = mock_store.complete_intent.call_args
        assert call_kwargs[1]["state"] == "failed"

    async def test_handler_generic_error(self) -> None:
        ev = IntentEvaluator()

        async def handler(_type: str, _payload: dict[str, object]) -> dict[str, object]:
            msg = "unexpected"
            raise RuntimeError(msg)

        ev.register_handler("test_intent", handler)

        with patch("theo.intent.evaluator.store") as mock_store:
            mock_store.complete_intent = AsyncMock()
            await ev._evaluate_one(1, "test_intent", {})

        call_kwargs = mock_store.complete_intent.call_args
        assert call_kwargs[1]["state"] == "failed"
        assert "unexpected" in call_kwargs[1]["error"]

    async def test_budget_exhausted_skips_cycle(self) -> None:
        """When daily budget is reached, evaluator skips fetching intents."""
        ev = IntentEvaluator()

        with (
            patch("theo.intent.evaluator.store") as mock_store,
            patch("theo.intent.evaluator.get_settings") as mock_settings,
        ):
            cfg = MagicMock()
            cfg.intent_evaluator_interval_s = 1
            cfg.intent_max_daily_budget_tokens = 1000
            mock_settings.return_value = cfg
            mock_store.expire_overdue = AsyncMock(return_value=[])
            mock_store.get_daily_token_usage = AsyncMock(return_value=1500)  # over budget
            mock_store.fetch_and_start = AsyncMock(return_value=None)

            await ev.start()
            await asyncio.sleep(0.1)
            await ev.stop()

            # fetch_next should never be called when over budget.
            mock_store.fetch_and_start.assert_not_called()

    async def test_active_throttle_defers_intents(self) -> None:
        """When foreground is active, evaluator defers intent processing."""
        ev = IntentEvaluator()
        ev.update_inflight(1)  # simulate active foreground

        with (
            patch("theo.intent.evaluator.store") as mock_store,
            patch("theo.intent.evaluator.get_settings") as mock_settings,
        ):
            cfg = MagicMock()
            cfg.intent_evaluator_interval_s = 1
            cfg.intent_max_daily_budget_tokens = 50000
            mock_settings.return_value = cfg
            mock_store.expire_overdue = AsyncMock(return_value=[])
            mock_store.get_daily_token_usage = AsyncMock(return_value=0)
            mock_store.fetch_and_start = AsyncMock(return_value=None)

            await ev.start()
            await asyncio.sleep(0.1)
            await ev.stop()

            # fetch_next should not be called when active.
            mock_store.fetch_and_start.assert_not_called()


# ── Date scanner tests ────────────────────────────────────────────────


class TestDateScanner:
    async def test_finds_approaching_iso_dates(self) -> None:
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
        context_body = {"upcoming": f"Meeting on {tomorrow}"}

        with (
            patch("theo.memory.core.read_one", new_callable=AsyncMock) as mock_read,
            patch("theo.intent.evaluator.store") as mock_store,
            patch("theo.intent.evaluator.get_settings") as mock_settings,
        ):
            cfg = MagicMock()
            cfg.intent_deadline_horizon_days = 7
            mock_settings.return_value = cfg

            doc = MagicMock()
            doc.body = context_body
            mock_read.return_value = doc
            mock_store.intent_exists = AsyncMock(return_value=False)
            mock_store.create_intent = AsyncMock(return_value=1)

            count = await scan_approaching_dates()
            assert count == 1
            mock_store.create_intent.assert_called_once()
            call_kwargs = mock_store.create_intent.call_args[1]
            assert call_kwargs["intent_type"] == "deadline_approaching"
            assert call_kwargs["base_priority"] == 80

    async def test_ignores_past_dates(self) -> None:
        past = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d")
        context_body = {"old": f"Event was on {past}"}

        with (
            patch("theo.memory.core.read_one", new_callable=AsyncMock) as mock_read,
            patch("theo.intent.evaluator.store") as mock_store,
            patch("theo.intent.evaluator.get_settings") as mock_settings,
        ):
            cfg = MagicMock()
            cfg.intent_deadline_horizon_days = 7
            mock_settings.return_value = cfg

            doc = MagicMock()
            doc.body = context_body
            mock_read.return_value = doc
            mock_store.create_intent = AsyncMock(return_value=1)

            count = await scan_approaching_dates()
            assert count == 0
            mock_store.create_intent.assert_not_called()

    async def test_ignores_far_future_dates(self) -> None:
        far = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%d")
        context_body = {"far": f"Conference on {far}"}

        with (
            patch("theo.memory.core.read_one", new_callable=AsyncMock) as mock_read,
            patch("theo.intent.evaluator.store") as mock_store,
            patch("theo.intent.evaluator.get_settings") as mock_settings,
        ):
            cfg = MagicMock()
            cfg.intent_deadline_horizon_days = 7
            mock_settings.return_value = cfg

            doc = MagicMock()
            doc.body = context_body
            mock_read.return_value = doc
            mock_store.create_intent = AsyncMock(return_value=1)

            count = await scan_approaching_dates()
            assert count == 0

    async def test_skips_existing_active_intents(self) -> None:
        """Date scanner skips dates that already have an active intent."""
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
        context_body = {"upcoming": f"Meeting on {tomorrow}"}

        with (
            patch("theo.memory.core.read_one", new_callable=AsyncMock) as mock_read,
            patch("theo.intent.evaluator.store") as mock_store,
            patch("theo.intent.evaluator.get_settings") as mock_settings,
        ):
            cfg = MagicMock()
            cfg.intent_deadline_horizon_days = 7
            mock_settings.return_value = cfg

            doc = MagicMock()
            doc.body = context_body
            mock_read.return_value = doc
            mock_store.intent_exists = AsyncMock(return_value=True)
            mock_store.create_intent = AsyncMock(return_value=1)

            count = await scan_approaching_dates()
            assert count == 0
            mock_store.create_intent.assert_not_called()

    async def test_handles_missing_context_doc(self) -> None:
        with (
            patch("theo.memory.core.read_one", new_callable=AsyncMock) as mock_read,
            patch("theo.intent.evaluator.get_settings") as mock_settings,
        ):
            cfg = MagicMock()
            cfg.intent_deadline_horizon_days = 7
            mock_settings.return_value = cfg
            mock_read.side_effect = LookupError

            count = await scan_approaching_dates()
            assert count == 0


# ── publish_intent wrapper tests ──────────────────────────────────────


class TestPublishIntent:
    async def test_publish_creates_and_wakes(self, mock_db: MagicMock) -> None:
        _ = mock_db
        from theo.intent import intent_evaluator, publish_intent  # noqa: PLC0415

        with patch.object(intent_evaluator, "wake", wraps=intent_evaluator.wake) as mock_wake:
            intent_id = await publish_intent(
                intent_type="test",
                source_module="tests",
            )
            assert intent_id == 1
            mock_wake.assert_called_once()
