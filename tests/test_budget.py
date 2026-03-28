"""Tests for budget controls and token tracking."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from theo.budget import (
    UsageRecord,
    _warned_bands,
    check_budget,
    estimate_cost,
    get_daily_total,
    get_session_total,
    record_usage,
)
from theo.bus.events import BudgetWarning
from theo.config import Settings
from theo.errors import BudgetExceededError

_REQUIRED = {
    "database_url": "postgresql://u:p@h:5432/d",
    "anthropic_api_key": "sk-test",
}

_CFG = Settings(**_REQUIRED, _env_file=None)  # type: ignore[arg-type]


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_REQUIRED, **overrides}, _env_file=None)  # type: ignore[arg-type]


def _record(**overrides: object) -> UsageRecord:
    defaults: dict[str, object] = {
        "session_id": uuid4(),
        "model": "test-model",
        "input_tokens": 10,
        "output_tokens": 5,
        "speed": "reactive",
    }
    return UsageRecord(**{**defaults, **overrides})  # type: ignore[arg-type]


# ── Fixtures ─────────────────────────────────────────────────────────


def _mock_pool() -> MagicMock:
    pool = MagicMock()
    pool.execute = AsyncMock()
    pool.fetchval = AsyncMock(return_value=0)
    return pool


@pytest.fixture
def mock_db():
    pool = _mock_pool()
    with patch("theo.budget.db") as mock:
        mock.pool = pool
        yield pool


@pytest.fixture
def mock_bus():
    published: list = []

    async def fake_publish(event):
        published.append(event)

    mock = MagicMock()
    mock.publish = AsyncMock(side_effect=fake_publish)
    with patch("theo.budget.bus", mock):
        yield mock, published


@pytest.fixture(autouse=True)
def _isolate_settings():
    _warned_bands.clear()
    with patch("theo.budget.get_settings", return_value=_CFG):
        yield


# ── Cost estimation tests ────────────────────────────────────────────


class TestEstimateCost:
    def test_reactive_cost(self) -> None:
        cost = estimate_cost(1000, 0, "reactive")
        assert cost == pytest.approx(0.25)

    def test_reflective_cost(self) -> None:
        cost = estimate_cost(500, 500, "reflective")
        assert cost == pytest.approx(3.0)

    def test_deliberative_cost(self) -> None:
        cost = estimate_cost(1000, 1000, "deliberative")
        assert cost == pytest.approx(30.0)

    def test_zero_tokens(self) -> None:
        cost = estimate_cost(0, 0, "reactive")
        assert cost == 0.0


# ── Recording tests ──────────────────────────────────────────────────


class TestRecordUsage:
    async def test_inserts_row(self, mock_db: MagicMock, mock_bus) -> None:
        _ = mock_bus
        session = uuid4()
        await record_usage(
            _record(
                session_id=session,
                model="claude-haiku-4-5-20251001",
                input_tokens=100,
                output_tokens=50,
            ),
        )
        mock_db.execute.assert_awaited_once()
        args = mock_db.execute.call_args[0]
        assert args[1] == session
        assert args[2] == "claude-haiku-4-5-20251001"
        assert args[3] == 100
        assert args[4] == 50

    async def test_records_correct_cost(self, mock_db: MagicMock, mock_bus) -> None:
        _ = mock_bus
        await record_usage(_record(input_tokens=1000, output_tokens=0))
        args = mock_db.execute.call_args[0]
        assert args[5] == pytest.approx(0.25)

    async def test_default_source_is_conversation(self, mock_db: MagicMock, mock_bus) -> None:
        _ = mock_bus
        await record_usage(_record())
        args = mock_db.execute.call_args[0]
        assert args[6] == "conversation"

    async def test_custom_source(self, mock_db: MagicMock, mock_bus) -> None:
        _ = mock_bus
        await record_usage(_record(source="deliberation"))
        args = mock_db.execute.call_args[0]
        assert args[6] == "deliberation"


# ── Budget warning tests ─────────────────────────────────────────────


class TestBudgetWarnings:
    async def test_daily_warning_emitted_at_threshold(self, mock_db: MagicMock, mock_bus) -> None:
        _bus, published = mock_bus
        mock_db.fetchval = AsyncMock(return_value=1_600_000)

        await record_usage(_record())

        warnings = [e for e in published if isinstance(e, BudgetWarning)]
        daily_warnings = [w for w in warnings if w.scope == "daily"]
        assert len(daily_warnings) == 1
        assert daily_warnings[0].usage_ratio >= 0.8

    async def test_session_warning_emitted_at_threshold(
        self, mock_db: MagicMock, mock_bus
    ) -> None:
        _bus, published = mock_bus
        session = uuid4()

        async def route_by_args(*args):
            if len(args) > 1:
                return 400_000  # session query (has session_id arg)
            return 100_000  # daily query (no args)

        mock_db.fetchval = AsyncMock(side_effect=route_by_args)

        await record_usage(_record(session_id=session))

        warnings = [e for e in published if isinstance(e, BudgetWarning)]
        session_warnings = [w for w in warnings if w.scope == "session"]
        assert len(session_warnings) == 1

    async def test_no_warning_below_threshold(self, mock_db: MagicMock, mock_bus) -> None:
        _bus, published = mock_bus
        mock_db.fetchval = AsyncMock(return_value=100)

        await record_usage(_record())

        warnings = [e for e in published if isinstance(e, BudgetWarning)]
        assert len(warnings) == 0

    async def test_duplicate_warning_suppressed(self, mock_db: MagicMock, mock_bus) -> None:
        """Same 5% band should not emit a second warning."""
        _bus, published = mock_bus
        mock_db.fetchval = AsyncMock(return_value=1_600_000)

        await record_usage(_record())
        await record_usage(_record())

        warnings = [e for e in published if isinstance(e, BudgetWarning) and e.scope == "daily"]
        assert len(warnings) == 1

    async def test_new_band_emits_warning(self, mock_db: MagicMock, mock_bus) -> None:
        """Crossing from 80% band to 85% band should emit a second warning."""
        _bus, published = mock_bus
        # First call at 80%
        mock_db.fetchval = AsyncMock(return_value=1_600_000)
        await record_usage(_record())
        # Second call at 85%
        mock_db.fetchval = AsyncMock(return_value=1_700_000)
        await record_usage(_record())

        warnings = [e for e in published if isinstance(e, BudgetWarning) and e.scope == "daily"]
        assert len(warnings) == 2


# ── Budget check tests ───────────────────────────────────────────────


class TestCheckBudget:
    async def test_passes_when_under_budget(self, mock_db: MagicMock) -> None:
        mock_db.fetchval = AsyncMock(return_value=100)
        await check_budget(uuid4())  # should not raise

    async def test_raises_when_daily_cap_exceeded(self, mock_db: MagicMock) -> None:
        mock_db.fetchval = AsyncMock(return_value=_CFG.budget_daily_cap_tokens)

        with pytest.raises(BudgetExceededError, match="Daily"):
            await check_budget(uuid4())

    async def test_raises_when_session_cap_exceeded(self, mock_db: MagicMock) -> None:
        session = uuid4()

        async def per_scope_fetchval(*args):
            if len(args) > 1:
                return _CFG.budget_session_cap_tokens  # session query
            return 0  # daily query

        mock_db.fetchval = AsyncMock(side_effect=per_scope_fetchval)

        with pytest.raises(BudgetExceededError, match="Session"):
            await check_budget(session)

    async def test_daily_checked_before_session(self, mock_db: MagicMock) -> None:
        """Daily cap is checked first — a daily breach should not query session."""
        mock_db.fetchval = AsyncMock(return_value=_CFG.budget_daily_cap_tokens)

        with pytest.raises(BudgetExceededError, match="Daily"):
            await check_budget(uuid4())

        # Only one fetchval call (daily), session not checked
        assert mock_db.fetchval.await_count == 1


# ── Aggregate query tests ────────────────────────────────────────────


class TestAggregateQueries:
    async def test_get_daily_total(self, mock_db: MagicMock) -> None:
        mock_db.fetchval = AsyncMock(return_value=12345)
        result = await get_daily_total()
        assert result == 12345

    async def test_get_session_total(self, mock_db: MagicMock) -> None:
        session = uuid4()
        mock_db.fetchval = AsyncMock(return_value=6789)
        result = await get_session_total(session)
        assert result == 6789
        mock_db.fetchval.assert_awaited_once()
        args = mock_db.fetchval.call_args[0]
        assert len(args) == 2  # query + session_id


# ── Config validation tests ──────────────────────────────────────────


class TestBudgetConfig:
    def test_default_budget_settings(self) -> None:
        cfg = _settings()
        assert cfg.budget_daily_cap_tokens == 2_000_000
        assert cfg.budget_session_cap_tokens == 500_000
        assert cfg.budget_warning_threshold == 0.8
        assert cfg.budget_cost_reactive_per_1k == 0.25
        assert cfg.budget_cost_reflective_per_1k == 3.0
        assert cfg.budget_cost_deliberative_per_1k == 15.0

    def test_daily_cap_zero_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="budget_daily_cap_tokens"):
            _settings(budget_daily_cap_tokens=0)

    def test_session_cap_zero_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="budget_session_cap_tokens"):
            _settings(budget_session_cap_tokens=0)

    def test_warning_threshold_zero_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="budget_warning_threshold"):
            _settings(budget_warning_threshold=0.0)

    def test_warning_threshold_one_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="budget_warning_threshold"):
            _settings(budget_warning_threshold=1.0)

    def test_custom_budget_settings(self) -> None:
        cfg = _settings(
            budget_daily_cap_tokens=100_000,
            budget_session_cap_tokens=10_000,
            budget_warning_threshold=0.5,
        )
        assert cfg.budget_daily_cap_tokens == 100_000
        assert cfg.budget_session_cap_tokens == 10_000
        assert cfg.budget_warning_threshold == 0.5


# ── Event model tests ────────────────────────────────────────────────


class TestBudgetWarningEvent:
    def test_budget_warning_is_ephemeral(self) -> None:
        event = BudgetWarning(
            scope="daily",
            used_tokens=1000,
            cap_tokens=2000,
            usage_ratio=0.5,
        )
        assert event.durable is False

    def test_budget_warning_fields(self) -> None:
        event = BudgetWarning(
            scope="session",
            used_tokens=400_000,
            cap_tokens=500_000,
            usage_ratio=0.8,
        )
        assert event.scope == "session"
        assert event.used_tokens == 400_000
        assert event.cap_tokens == 500_000
        assert event.usage_ratio == 0.8

    def test_budget_warning_serialization_roundtrip(self) -> None:
        original = BudgetWarning(
            scope="daily",
            used_tokens=1_600_000,
            cap_tokens=2_000_000,
            usage_ratio=0.8,
            session_id=uuid4(),
        )
        restored = BudgetWarning.model_validate_json(original.model_dump_json())
        assert restored.scope == original.scope
        assert restored.used_tokens == original.used_tokens
        assert restored.usage_ratio == original.usage_ratio
