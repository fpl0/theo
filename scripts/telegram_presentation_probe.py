"""Explicit synthetic presentation probe using the real Telegram transport and ledger."""

import asyncio
import contextlib
import fcntl
import os

from theo.channels.telegram.adapter import Telegram
from theo.config import Settings
from theo.delivery.ledger import Delivery
from theo.domain import Json, Outcome, uid
from theo.storage import Database
from theo.work.jobs import Jobs


async def presentation_probe(db: Database, settings: Settings, token: str) -> None:
    lock = (db.root / "daemon.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    telegram = Telegram(db, settings, token)
    poll_task = None
    job = None
    try:
        await telegram.setup()
        conversation = await telegram.state.destination(settings.telegram_chat_id or 0)
        assert conversation is not None
        jobs = Jobs(db, settings.owner_id)
        key = "presentation-probe:" + uid()

        async def send(operation: str, payload: Json) -> Json:
            payload.pop("_channel", None)
            return await telegram.send(operation, payload)

        async def final(text: str, suffix: str) -> None:
            action = await Delivery(db, settings).prepare(
                conversation,
                "send_message",
                {"text": text},
                key + suffix,
                durable_obligation=True,
            )
            for _ in range(40):
                await Delivery(db, settings).dispatch_one(send)
                result = await db.one("SELECT status FROM actions WHERE id=?", (action,))
                if result and result["status"] == "succeeded":
                    return
                await asyncio.sleep(0.25)
            raise RuntimeError("Probe delivery did not confirm; inspect the ledger before retrying")

        await final(
            "Live Telegram transport check — synthetic text.\n"
            "First a typing indicator, then a growing draft. No model is being called.",
            ":intro",
        )
        job_id = await jobs.enqueue(
            conversation,
            "reminder",
            {"text": "Synthetic presentation probe", "diagnostic": True},
            key,
            lane="interactive",
        )
        job = await jobs.claim("interactive", str(os.getpid()), reminders_only=True)
        if not job or job["id"] != job_id:
            if job:
                await jobs.finish(
                    job["id"], job["generation"], Outcome.INTERRUPTED, {"diagnostic_deferred": True}
                )
                job = None
            await jobs.cancel(job_id)
            raise RuntimeError("Probe could not claim its own notification job")

        async def poll() -> None:
            while True:
                await telegram.poll_once()

        poll_task = asyncio.create_task(poll())
        for _ in range(12):
            await telegram.preview(job)
            await asyncio.sleep(1)
        fragments = [
            "Synthetic streaming check.\n\n",
            "This text is arriving in a native Telegram draft. ",
            "It should grow inside one temporary message. ",
            "The final message uses Theo's delivery ledger.\n\n",
            "You can use Telegram's Stop control while this draft is active. ",
            "This is transport evidence only; native model qualification remains pending. ",
        ]
        for fragment in fragments:
            current = await db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
            if current and current["status"] == "cancelled":
                break
            await jobs.heartbeat(job_id, job["generation"])
            await telegram.preview(job, fragment)
            await asyncio.sleep(4)
        current = await db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
        cancelled = bool(current and current["status"] == "cancelled")
        if not cancelled:
            await jobs.finish(job_id, job["generation"], Outcome.COMPLETED, {"synthetic": True})
        await telegram.end_preview(job_id)
        await final(
            "Synthetic stream cancelled through Telegram. No model was called."
            if cancelled
            else "".join(fragments) + "\n\nTransport check finished.",
            ":result",
        )
        print(f"Presentation probe finished: cancelled={cancelled}; job={job_id}", flush=True)
    finally:
        if poll_task:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
        if job:
            current = await db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))
            if current and current["status"] == "running":
                await Jobs(db, settings.owner_id).cancel(job["id"])
        await telegram.close()
        lock.close()
