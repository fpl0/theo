"""Compose and run the Theo daemon and its background loops.

Owns the daemon lock, broker socket, signal handling, recovery, channel polling,
outbox dispatch and periodic maintenance; delegates job execution to Coordinator.
"""

import asyncio
import contextlib
import fcntl
import os
import signal
import tempfile
from pathlib import Path

from theo.application.coordinator import Coordinator
from theo.channels.telegram.adapter import Telegram
from theo.channels.telegram.controls import TelegramUI
from theo.config import Settings
from theo.delivery.ledger import Delivery
from theo.domain import (
    Denied,
    Json,
    encode,
    uid,
)
from theo.observability import telemetry
from theo.operations.backups import backup_create, retain_backups
from theo.storage import Database
from theo.tools.broker import ToolBroker
from theo.work.autonomy import Autonomy
from theo.work.improvement import Critic
from theo.work.jobs import Jobs
from theo.work.scheduling import Scheduler


async def serve(db: Database, settings: Settings, token: str | None = None) -> None:
    telemetry.configure(db.root)
    lock = (db.root / "daemon.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise Denied("Theo already has a daemon for this data root") from None
    broker = ToolBroker(db, settings)
    # Darwin's sockaddr_un has a 104-byte path limit. Data roots and the default
    # macOS temporary directory can both exceed it. A private /tmp directory is
    # safe for host-only mode; configured workers retain their own home boundary.
    socket_path = (
        Path(tempfile.mkdtemp(prefix="theo-", dir=settings.worker_home or "/tmp")) / "broker.sock"
    )
    try:
        if len(os.fsencode(socket_path)) >= 104:
            raise Denied("Native runner home is too long for a portable Unix socket path")
        await broker.listen(socket_path)
    except BaseException:
        await broker.close()
        socket_path.unlink(missing_ok=True)
        socket_path.parent.rmdir()
        lock.close()
        raise
    telegram = (
        Telegram(db, settings, token)
        if token
        and settings.telegram_owner_id is not None
        and settings.telegram_chat_id is not None
        else None
    )
    coordinator = Coordinator(db, settings, broker, socket_path, telegram=telegram)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    tasks: set[asyncio.Task[None]] = set()
    lifecycle = uid()
    from theo.execution.registry import terminate_registered

    await terminate_registered(db, settings.owner_id)
    await Jobs(db, settings.owner_id).recover()
    await db.execute(
        "INSERT INTO lifecycle_intervals VALUES(?,?,?,?,?,?)",
        (lifecycle, settings.owner_id, db.clock(), None, db.clock(), 0),
    )

    async def poll() -> None:
        assert telegram
        while not stop.is_set():
            try:
                if not telegram.username:
                    await telegram.setup()
                    await telegram.state.destination(settings.telegram_chat_id or 0)
                    for destination in settings.telegram_destinations:
                        await telegram.state.destination(destination.chat_id, destination.topic_id)
                await telegram.poll_once()
            except Exception as exc:
                await db.health(settings.owner_id, "poll_error", {"error": type(exc).__name__})
                try:
                    await asyncio.wait_for(stop.wait(), 5)
                except TimeoutError:
                    pass

    async def sender(operation: str, payload: Json) -> Json:
        channel = payload.pop("_channel", "local")
        if telegram and channel == "telegram":
            return await telegram.send(operation, payload)
        if channel == "telegram":
            from theo.delivery.contracts import NoEffect

            raise NoEffect("telegram_credentials_unavailable", retry_after=60)
        print(encode({"operation": operation, "payload": payload}), flush=True)
        return {"message_id": uid(), "channel": "local_stdout"}

    poll_task = asyncio.create_task(poll()) if telegram else None

    async def dispatch() -> None:
        while not stop.is_set():
            try:
                if await Delivery(db, settings).dispatch_one(sender):
                    continue
            except Exception as exc:
                await db.health(settings.owner_id, "dispatch_error", {"error": type(exc).__name__})
            try:
                await asyncio.wait_for(stop.wait(), 0.5)
            except TimeoutError:
                pass

    dispatch_task = asyncio.create_task(dispatch())

    async def repair_embeddings() -> None:
        while not stop.is_set():
            try:
                if (db.root / "models/embeddings/manifest.json").exists():
                    await coordinator.embeddings.repair_one()
            except Exception as exc:
                await db.health(
                    settings.owner_id, "embedding_repair_error", {"error": type(exc).__name__}
                )
            try:
                await asyncio.wait_for(stop.wait(), 2)
            except TimeoutError:
                pass

    embedding_task = asyncio.create_task(repair_embeddings())
    last_maintenance = 0.0
    last_backup = db.clock()
    try:
        while not stop.is_set():
            if telegram:
                await telegram.process_pending()
                if db.clock() - last_maintenance >= 30:
                    await TelegramUI(db, settings).reviews()
            await coordinator.commands()
            await coordinator.reconcile_cancellations()
            await Scheduler(db, settings.owner_id).tick()
            await Scheduler(db, settings.owner_id).deliver_reminders(settings)
            background_paused = await db.control(settings.owner_id, "background_paused") == "true"
            for lane in ("interactive", "background"):
                job = await Jobs(db, settings.owner_id).claim(
                    lane,
                    str(os.getpid()),
                    max_total=settings.max_runs,
                    max_background=settings.max_background,
                    reminders_only=background_paused and lane == "background",
                )
                if job:
                    task = asyncio.create_task(coordinator.run_job(job))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
            if db.clock() - last_maintenance >= 30:
                prior = await db.one(
                    "SELECT deliberate_pause FROM lifecycle_intervals WHERE id=?", (lifecycle,)
                )
                if prior and prior["deliberate_pause"] != int(background_paused):
                    await db.execute(
                        "UPDATE lifecycle_intervals SET ended_at=?,heartbeat_at=? WHERE id=?",
                        (db.clock(), db.clock(), lifecycle),
                    )
                    lifecycle = uid()
                    await db.execute(
                        "INSERT INTO lifecycle_intervals VALUES(?,?,?,?,?,?)",
                        (
                            lifecycle,
                            settings.owner_id,
                            db.clock(),
                            None,
                            db.clock(),
                            int(background_paused),
                        ),
                    )
                else:
                    await db.execute(
                        "UPDATE lifecycle_intervals SET heartbeat_at=? WHERE id=?",
                        (db.clock(), lifecycle),
                    )
                (db.root / "heartbeat.json").write_text(
                    encode(
                        {
                            "timestamp": db.clock(),
                            "pid": os.getpid(),
                            "deliberate_pause": background_paused,
                        }
                    )
                )
                telemetry.measure("theo_runtime_telemetry_timestamp", db.clock(), gauge=True)
                conversation = await db.conversation(
                    settings.owner_id,
                    "telegram" if telegram else "local",
                    str(settings.telegram_chat_id) if telegram else settings.owner_id,
                )
                await Autonomy(db, settings.owner_id).tick(conversation)
                await Critic(db, settings.owner_id).queue()
                last_maintenance = db.clock()
            if db.clock() - last_backup >= 3600:
                try:
                    await backup_create(db, settings)
                    retain_backups(db.root / "backups")
                except Exception as exc:
                    await db.health(
                        settings.owner_id, "backup_failed", {"error": type(exc).__name__}
                    )
                last_backup = db.clock()
            try:
                await asyncio.wait_for(stop.wait(), 1)
            except TimeoutError:
                pass
    finally:
        embedding_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await embedding_task
        dispatch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dispatch_task
        if poll_task:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await broker.close()
        if telegram:
            await telegram.close()
        await db.execute(
            "UPDATE lifecycle_intervals SET ended_at=?,heartbeat_at=? WHERE id=?",
            (db.clock(), db.clock(), lifecycle),
        )
        socket_path.unlink(missing_ok=True)
        socket_path.parent.rmdir()
        lock.close()
