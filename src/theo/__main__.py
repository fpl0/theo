"""python -m theo entrypoint."""

import asyncio
import logging
import signal

from dotenv import load_dotenv

from theo.config import get_settings
from theo.db import db
from theo.db.migrate import migrate
from theo.telemetry import init_telemetry, shutdown_telemetry

log = logging.getLogger("theo")


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

    try:
        await db.connect()
        await migrate(db)
        log.info("database ready")

        # Start intent evaluator if enabled.
        if cfg.intent_evaluator_enabled:
            from theo.intent import intent_evaluator  # noqa: PLC0415

            await intent_evaluator.start()
            log.info("intent evaluator ready")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        log.info("running — press Ctrl-C to stop")
        await stop.wait()
    finally:
        log.info("shutting down")

        # Stop intent evaluator before closing the pool.
        if cfg.intent_evaluator_enabled:
            from theo.intent import intent_evaluator  # noqa: PLC0415

            await intent_evaluator.stop()

        await db.close()
        shutdown_telemetry()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
