"""Tests for the application lifecycle (__main__.py)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from theo.__main__ import _DRAIN_TIMEOUT_S, _log_banner, _shutdown, _startup, _validate_config
from theo.config import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# ── Helpers ──────────────────────────────────────────────────────────


def _make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "database_url": "postgresql://localhost/test",
        "anthropic_api_key": "sk-test",
        "telegram_bot_token": "bot-token",
        "telegram_owner_chat_id": 12345,
        "otel_enabled": False,
    }
    defaults.update(overrides)
    return Settings(**defaults, _env_file=None)  # type: ignore[arg-type]


# ── _validate_config ─────────────────────────────────────────────────


class TestValidateConfig:
    def test_exits_when_telegram_token_missing(self) -> None:
        cfg = _make_settings(telegram_bot_token=None)
        with pytest.raises(SystemExit):
            _validate_config(cfg)

    def test_exits_when_owner_chat_id_missing(self) -> None:
        cfg = _make_settings(telegram_owner_chat_id=None)
        with pytest.raises(SystemExit):
            _validate_config(cfg)

    def test_exits_when_both_missing(self) -> None:
        cfg = _make_settings(telegram_bot_token=None, telegram_owner_chat_id=None)
        with pytest.raises(SystemExit):
            _validate_config(cfg)

    def test_passes_when_config_complete(self) -> None:
        cfg = _make_settings()
        _validate_config(cfg)  # should not raise


# ── _log_banner ──────────────────────────────────────────────────────


class TestLogBanner:
    def test_logs_version_and_models(self, caplog: pytest.LogCaptureFixture) -> None:
        cfg = _make_settings()
        with caplog.at_level("INFO", logger="theo"):
            _log_banner(cfg)

        assert "startup complete" in caplog.text


# ── _startup ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_components() -> AsyncGenerator[dict[str, MagicMock]]:
    """Patch all external components for lifecycle tests."""
    mock_db = MagicMock()
    mock_db.connect = AsyncMock()
    mock_db.close = AsyncMock()
    mock_db.pool = MagicMock()
    mock_db.pool.execute = AsyncMock(return_value="SELECT 1")

    mock_bus = MagicMock()
    mock_bus.start = AsyncMock()
    mock_bus.stop = AsyncMock()
    mock_bus.subscribe = MagicMock()

    mock_engine_instance = MagicMock()
    mock_engine_instance.start = AsyncMock()
    mock_engine_instance.stop = AsyncMock()
    mock_engine_instance.kill = MagicMock()
    mock_engine_cls = MagicMock(return_value=mock_engine_instance)

    mock_gate_instance = MagicMock()
    mock_gate_instance.start = AsyncMock()
    mock_gate_instance.stop = AsyncMock()
    mock_gate_cls = MagicMock(return_value=mock_gate_instance)

    mock_migrate = AsyncMock()

    mock_circuit = MagicMock()
    mock_circuit.state = "closed"

    mock_queue = MagicMock()
    mock_queue.depth = 0

    mock_health = AsyncMock(
        return_value=MagicMock(
            db_connected=True,
            api_reachable=True,
            telegram_connected=True,
            circuit_state="closed",
            retry_queue_depth=0,
        )
    )

    mocks = {
        "db": mock_db,
        "bus": mock_bus,
        "engine_cls": mock_engine_cls,
        "engine": mock_engine_instance,
        "gate_cls": mock_gate_cls,
        "gate": mock_gate_instance,
        "migrate": mock_migrate,
        "circuit": mock_circuit,
        "queue": mock_queue,
        "health_check": mock_health,
    }

    with (
        patch("theo.__main__.db", mock_db),
        patch("theo.__main__.bus", mock_bus),
        patch("theo.__main__.ConversationEngine", mock_engine_cls),
        patch("theo.__main__.TelegramGate", mock_gate_cls),
        patch("theo.__main__.migrate", mock_migrate),
        patch("theo.__main__.circuit_breaker", mock_circuit),
        patch("theo.__main__.retry_queue", mock_queue),
        patch("theo.__main__.health_check", mock_health),
    ):
        yield mocks


class TestStartup:
    async def test_starts_components_in_order(
        self,
        mock_components: dict[str, MagicMock],
    ) -> None:
        cfg = _make_settings()
        engine, gate = await _startup(cfg)

        m = mock_components
        m["db"].connect.assert_awaited_once()
        m["migrate"].assert_awaited_once()
        m["bus"].start.assert_awaited_once()
        m["engine"].start.assert_awaited_once()
        m["gate"].start.assert_awaited_once()
        m["health_check"].assert_awaited_once()

        assert engine is m["engine"]
        assert gate is m["gate"]

    async def test_gate_receives_engine(
        self,
        mock_components: dict[str, MagicMock],
    ) -> None:
        cfg = _make_settings()
        await _startup(cfg)

        m = mock_components
        m["gate_cls"].assert_called_once_with(engine=m["engine"])

    async def test_health_check_warnings_logged(
        self,
        mock_components: dict[str, MagicMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        m = mock_components
        m["health_check"].return_value = MagicMock(
            db_connected=False,
            api_reachable=False,
            telegram_connected=True,
            circuit_state="open",
            retry_queue_depth=0,
        )

        cfg = _make_settings()
        with caplog.at_level("WARNING", logger="theo"):
            await _startup(cfg)

        assert "database unreachable" in caplog.text
        assert "API unreachable" in caplog.text


# ── _shutdown ────────────────────────────────────────────────────────


class TestShutdown:
    async def test_stops_components_in_reverse_order(
        self,
        mock_components: dict[str, MagicMock],
    ) -> None:
        m = mock_components
        call_order: list[str] = []

        m["gate"].stop = AsyncMock(side_effect=lambda: call_order.append("gate"))
        m["engine"].stop = AsyncMock(side_effect=lambda: call_order.append("engine"))
        m["bus"].stop = AsyncMock(side_effect=lambda: call_order.append("bus"))
        m["db"].close = AsyncMock(side_effect=lambda: call_order.append("db"))

        await _shutdown(gate=m["gate"], engine=m["engine"])

        assert call_order == ["gate", "engine", "bus", "db"]

    async def test_engine_drain_timeout_triggers_kill(
        self,
        mock_components: dict[str, MagicMock],
    ) -> None:
        m = mock_components

        async def slow_stop() -> None:
            await asyncio.sleep(_DRAIN_TIMEOUT_S + 1)

        m["engine"].stop = AsyncMock(side_effect=slow_stop)

        with patch("theo.__main__._DRAIN_TIMEOUT_S", 0.05):
            await _shutdown(gate=m["gate"], engine=m["engine"])

        m["engine"].kill.assert_called_once()

    async def test_shutdown_handles_none_components(
        self,
        mock_components: dict[str, MagicMock],
    ) -> None:
        m = mock_components
        await _shutdown(gate=None, engine=None)

        m["bus"].stop.assert_awaited_once()
        m["db"].close.assert_awaited_once()
