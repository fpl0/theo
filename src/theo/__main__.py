"""python -m theo entrypoint."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import TYPE_CHECKING

from dotenv import load_dotenv
from opentelemetry import trace

from theo import __version__
from theo.bus import bus
from theo.config import get_settings
from theo.conversation import ConversationEngine
from theo.db import db
from theo.db.migrate import migrate
from theo.gates.telegram import TelegramGate
from theo.memory.nodes import drain_background_tasks
from theo.resilience import circuit_breaker, health_check, retry_queue
from theo.telemetry import init_telemetry, shutdown_telemetry

if TYPE_CHECKING:
    from theo.config import Settings

log = logging.getLogger("theo")
tracer = trace.get_tracer(__name__)

_DRAIN_TIMEOUT_S = 30.0


def _validate_config(cfg: Settings) -> None:
    """Fail fast if essential configuration is missing.

    Raises :class:`SystemExit` with a clear message listing every
    missing variable so the operator can fix them all at once.
    """
    missing: list[str] = []
    if cfg.telegram_bot_token is None:
        missing.append("THEO_TELEGRAM_BOT_TOKEN")
    if cfg.telegram_owner_chat_id is None:
        missing.append("THEO_TELEGRAM_OWNER_CHAT_ID")
    if missing:
        log.critical("missing required config: %s", ", ".join(missing))
        sys.exit(1)


def _log_banner(cfg: Settings) -> None:
    """Log the startup banner."""
    log.info(
        "startup complete",
        extra={
            "version": __version__,
            "model.reactive": cfg.llm_model_reactive,
            "model.reflective": cfg.llm_model_reflective,
            "model.deliberative": cfg.llm_model_deliberative,
            "owner.chat_id": cfg.telegram_owner_chat_id,
        },
    )


async def _startup(cfg: Settings) -> tuple[ConversationEngine, TelegramGate]:
    """Start all components in order. Returns ``(engine, gate)``."""
    with tracer.start_as_current_span("lifecycle.startup"):
        await db.connect()
        await migrate(db)
        log.info("database ready")

        await bus.start()
        log.info("event bus ready")

        engine = ConversationEngine()
        await engine.start()

        gate = TelegramGate(engine=engine)
        await gate.start()

        _log_banner(cfg)

        status = await health_check(
            circuit=circuit_breaker,
            queue=retry_queue,
        )
        if not status.db_connected:
            log.warning("health check: database unreachable")
        if not status.api_reachable:
            log.warning("health check: API unreachable (circuit not closed)")

    return engine, gate


async def _shutdown(
    *,
    gate: TelegramGate | None,
    engine: ConversationEngine | None,
) -> None:
    """Stop all components in reverse startup order."""
    with tracer.start_as_current_span("lifecycle.shutdown"):
        log.info("shutting down")

        if gate is not None:
            try:
                await gate.stop()
            except Exception:
                log.exception("gate stop failed")

        if engine is not None:
            try:
                await asyncio.wait_for(engine.stop(), timeout=_DRAIN_TIMEOUT_S)
            except TimeoutError:
                log.warning("engine drain timed out after %.0fs, killing", _DRAIN_TIMEOUT_S)
                engine.kill()
            except Exception:
                log.exception("engine stop failed")

        try:
            await drain_background_tasks()
        except Exception:
            log.exception("background task drain failed")

        await bus.stop()
        await db.close()
        shutdown_telemetry()


async def _run() -> None:
    # Load env files into the process so non-THEO_ vars
    # (OTEL_*, PYROSCOPE_*) are visible to their SDKs.
    load_dotenv(".env")
    load_dotenv(".env.local", override=True)

    cfg = get_settings()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s │ %(levelname)-8s │ %(name)-20s │ %(message)s",
        datefmt="%H:%M:%S",
    )

    if cfg.otel_enabled:
        init_telemetry()

    log.info("starting")
    _validate_config(cfg)

    gate: TelegramGate | None = None
    engine: ConversationEngine | None = None
    try:
        engine, gate = await _startup(cfg)

        # Signal handling with double-Ctrl-C force exit.
        stop = asyncio.Event()

        def _on_signal() -> None:
            if stop.is_set():
                log.warning("forced exit (double signal)")
                os._exit(1)
            stop.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _on_signal)

        log.info("running — press Ctrl-C to stop")
        await stop.wait()
    finally:
        await _shutdown(gate=gate, engine=engine)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
