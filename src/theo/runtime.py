"""Host-owned coordination of canonical context, native workers and final obligations."""

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path

from theo.autonomy import CADENCES, Autonomy
from theo.backends import backend_for
from theo.backends.native import NativeBackend
from theo.channels import Telegram
from theo.config import Settings
from theo.context import ContextAssembler
from theo.delivery import Delivery
from theo.domain import (
    Denied,
    ExecutionOutcome,
    ExecutionRequest,
    InputPart,
    Json,
    Outcome,
    ToolContext,
    encode,
    uid,
)
from theo.embeddings import Embeddings
from theo.improvement import Critic
from theo.jobs import Jobs
from theo.operations import backup_create, retain_backups
from theo.scheduling import Scheduler
from theo.storage import Database
from theo.tools import REGISTRY, ToolBroker


class Coordinator:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        broker: ToolBroker,
        socket_path: Path,
        factory: Callable[[str], NativeBackend] | None = None,
        telegram: Telegram | None = None,
    ):
        self.db, self.settings, self.broker, self.socket_path = db, settings, broker, socket_path
        self.owner = settings.owner_id
        self.jobs = Jobs(db, self.owner)
        self.factory: Callable[[str], NativeBackend] = factory or (
            lambda name: backend_for(name, db=db, settings=settings)
        )
        self.telegram = telegram
        self.embeddings = Embeddings(db, self.owner)
        self.active: dict[str, tuple[NativeBackend | None, str]] = {}

    async def run_job(self, job: Json) -> None:
        payload = json.loads(job["payload"])
        run_id = uid()
        conversation = await self.db.one(
            "SELECT * FROM conversations WHERE id=? AND owner_id=?",
            (job["conversation_id"], self.owner),
        )
        assert conversation
        route = payload.get("backend") or conversation["backend"] or self.settings.primary_backend
        model = payload.get("model") or conversation["model"] or self.settings.primary_model
        await self.db.execute(
            "INSERT INTO runs(id,owner_id,job_id,generation,backend,model,status,started_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                run_id,
                self.owner,
                job["id"],
                job["generation"],
                route or "none",
                model or "none",
                "running",
                self.db.clock(),
            ),
        )
        self.active[job["id"]] = (None, run_id)
        context: Json | None = None
        heartbeat: asyncio.Task[None] | None = None
        try:
            if job["kind"] == "reminder":
                text = str(payload["text"])
                if payload.get("late_seconds", 0) > 3600:
                    text += "\n(This reminder is late because Theo was unavailable.)"
                outcome = ExecutionOutcome(status=Outcome.COMPLETED, text=text)
            elif not route or not model:
                outcome = ExecutionOutcome(
                    status=Outcome.AUTH, error="Select a verified subscription backend and model"
                )
            elif await self.db.control(self.owner, "models_paused") == "true":
                outcome = ExecutionOutcome(
                    status=Outcome.INTERRUPTED, error="Model execution deliberately paused"
                )
            else:
                parts = payload.get("parts", [])
                if self.telegram:
                    parts = [await self.telegram.hydrate(part) for part in parts]
                    payload["parts"] = parts
                    await self.db.execute(
                        "UPDATE jobs SET payload=? WHERE id=?", (encode(payload), job["id"])
                    )
                text = str(payload.get("text", ""))
                if parts:
                    text += "\nINPUT PARTS\n" + encode(parts)
                semantic = None
                try:
                    semantic = await self.embeddings.search(text)
                except Exception:
                    pass  # durable embedding repair is separate; retain all user media/input
                context = await ContextAssembler(
                    self.db, self.owner, self.settings.context_window
                ).assemble(
                    job["conversation_id"],
                    text,
                    "deep" if job["kind"] == "deep_work" else "standard",
                    semantic,
                )
                await self.db.execute(
                    "UPDATE runs SET context_id=? WHERE id=?", (context["id"], run_id)
                )
                workspace_base = (
                    self.settings.worker_home / "workspaces"
                    if self.settings.worker_home
                    else self.db.root.parent / (self.db.root.name + "-workspaces")
                )
                workspace = workspace_base / job["id"]
                workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.settings.runner_uid is not None and os.geteuid() == 0:
                    os.chown(workspace, self.settings.runner_uid, self.settings.runner_gid or -1)
                tool_context = ToolContext(
                    owner_id=self.owner,
                    conversation_id=job["conversation_id"],
                    job_id=job["id"],
                    run_id=run_id,
                    generation=job["generation"],
                    workspace=workspace,
                    tools=frozenset(
                        {
                            "recall",
                            "recall_conversation",
                            "memory_history",
                            "action_status",
                            "file_read",
                        }
                    )
                    if job["kind"] == "critic"
                    else frozenset(REGISTRY),
                )
                token = self.broker.grant(tool_context)
                request = ExecutionRequest(
                    run_id=run_id,
                    job_id=job["id"],
                    conversation_id=job["conversation_id"],
                    owner_id=self.owner,
                    backend=route,
                    model=model,
                    lane=job["lane"],
                    context=context["rendered"],
                    parts=tuple(InputPart.model_validate(x) for x in parts),
                    workspace=workspace,
                    deadline=min(job["deadline"], self.db.clock() + 60)
                    if job["kind"] == "critic"
                    else job["deadline"],
                    generation=job["generation"],
                    tool_socket=str(self.socket_path),
                    tool_token=token,
                )
                backend = self.factory(route)
                self.active[job["id"]] = (backend, run_id)
                heartbeat = asyncio.create_task(self._heartbeat(job))
                outcome = ExecutionOutcome(
                    status=Outcome.FAILED, error="Native runtime produced no terminal event"
                )
                async for event in backend.events(request):
                    if event.kind == "terminal":
                        outcome = ExecutionOutcome.model_validate(event.payload)
                    # Preserve event type and bounded observable data; no hidden reasoning or raw secrets.
                    event_payload = (
                        event.payload
                        if event.kind != "text_delta"
                        else {"characters": len(str(event.payload.get("text", "")))}
                    )
                    await self.db.execute(
                        "INSERT OR IGNORE INTO run_events VALUES(?,?,?,?,?,?,?)",
                        (
                            event.event_id,
                            self.owner,
                            run_id,
                            event.sequence,
                            event.kind,
                            encode(event_payload),
                            event.timestamp,
                        ),
                    )
            if job["kind"] == "critic" and outcome.status == Outcome.COMPLETED:
                await Critic(self.db, self.owner).record(
                    payload["action_id"], payload["request_hash"], outcome.text
                )
            await self.commit_outcome(job, run_id, outcome, context)
        except asyncio.CancelledError:
            with contextlib.suppress(Denied):
                await self.commit_outcome(
                    job,
                    run_id,
                    ExecutionOutcome(status=Outcome.INTERRUPTED, error="Deliberate shutdown"),
                    context,
                )
            raise
        except Denied:
            await self.db.execute(
                "UPDATE runs SET status='cancelled',ended_at=? WHERE id=? AND status='running'",
                (self.db.clock(), run_id),
            )
        except Exception as exc:
            with contextlib.suppress(Denied):
                await self.commit_outcome(
                    job,
                    run_id,
                    ExecutionOutcome(
                        status=Outcome.FAILED, error=f"Host execution failed: {type(exc).__name__}"
                    ),
                    context,
                )
        finally:
            self.broker.revoke(run_id)
            if heartbeat:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError, Denied):
                    await heartbeat
            backend, _ = self.active.pop(job["id"], (None, ""))
            if backend:
                await backend.cancel()
            await self.db.execute("DELETE FROM worker_processes WHERE run_id=?", (run_id,))

    async def _heartbeat(self, job: Json) -> None:
        while True:
            await asyncio.sleep(15)
            await self.jobs.heartbeat(job["id"], job["generation"])

    async def commit_outcome(
        self, job: Json, run_id: str, outcome: ExecutionOutcome, context: Json | None
    ) -> None:
        def commit(db: sqlite3.Connection) -> None:
            self.jobs.check(db, job["id"], job["generation"])
            final_key = f"final:{job['id']}"
            existing = db.execute(
                "SELECT id,status FROM actions WHERE owner_id=? AND semantic_key=?",
                (self.owner, final_key),
            ).fetchone()
            status = outcome.status
            text = outcome.text.strip()
            if status == Outcome.COMPLETED and not text and not existing:
                status = Outcome.FAILED
                text = "Theo finished this attempt without a useful result. The job remains visible for inspection."
            final_required = job["kind"] in ("conversation", "delegated", "reminder", "deep_work")
            if status in (Outcome.AUTH, Outcome.QUOTA):
                text = outcome.error or "Work is waiting for an eligible subscription account."
            elif status == Outcome.FAILED and not text:
                text = outcome.error or "This attempt failed; no completion is claimed."
            if (
                final_required
                and text
                and not existing
                and status not in (Outcome.INTERRUPTED, Outcome.CANCELLED)
            ):
                freshness = (
                    {item["id"]: item["revision"] for item in context["sources"]["facts"]}
                    if context
                    else {}
                )
                key = (
                    final_key
                    if status in (Outcome.COMPLETED, Outcome.FAILED)
                    else f"waiting:{job['id']}:{status.value}"
                )
                Delivery(self.db, self.settings).prepare_in(
                    db,
                    job["conversation_id"],
                    "send_message",
                    {"text": text},
                    key,
                    job_id=job["id"],
                    run_id=run_id,
                    generation=job["generation"],
                    autonomous=job["lane"] == "background",
                    role="final" if status in (Outcome.COMPLETED, Outcome.FAILED) else "progress",
                    freshness=freshness,
                    durable_obligation=job["kind"] == "reminder",
                )
            if (
                job["kind"] in CADENCES
                and job["kind"] != "deep_work"
                and status == Outcome.COMPLETED
                and text
            ):
                db.execute(
                    "INSERT OR IGNORE INTO proposals VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        uid(),
                        self.owner,
                        job["kind"],
                        job["semantic_key"],
                        job["payload"],
                        text,
                        "proposed",
                        None,
                        self.db.clock(),
                    ),
                )
            db.execute(
                "UPDATE runs SET status=?,output=?,error=?,input_tokens=?,output_tokens=?,ended_at=? WHERE id=?",
                (
                    status.value,
                    outcome.text,
                    outcome.error,
                    outcome.input_tokens,
                    outcome.output_tokens,
                    self.db.clock(),
                    run_id,
                ),
            )
            db.execute(
                "UPDATE jobs SET status=?,outcome=?,lease_until=NULL,available_at=?,updated_at=? WHERE id=?",
                (
                    status.value,
                    encode(outcome.model_dump(mode="json")),
                    self.db.clock() + 300
                    if status in (Outcome.AUTH, Outcome.QUOTA)
                    else self.db.clock(),
                    self.db.clock(),
                    job["id"],
                ),
            )
            db.execute("DELETE FROM resource_claims WHERE job_id=?", (job["id"],))
            accounts = db.execute(
                "SELECT id FROM backend_accounts WHERE owner_id=? AND backend=(SELECT backend FROM runs WHERE id=?) AND status='verified' ORDER BY verified_at DESC LIMIT 1",
                (self.owner, run_id),
            ).fetchone()
            if accounts:
                db.execute(
                    "INSERT INTO usage_observations VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        uid(),
                        self.owner,
                        run_id,
                        accounts[0],
                        outcome.input_tokens,
                        outcome.output_tokens,
                        None,
                        None,
                        None,
                        self.db.clock(),
                    ),
                )

        await self.db.write(commit)

    async def cancel(self, job_id: str) -> None:
        children = await self.jobs.cancel(job_id)
        for child in children:
            if child in self.active:
                backend, run_id = self.active[child]
                self.broker.revoke(run_id)
                if backend:
                    await backend.cancel()

    async def commands(self) -> None:
        jobs = await self.db.read(
            "SELECT * FROM jobs WHERE owner_id=? AND status='queued' AND kind='conversation' AND json_extract(payload,'$.text') LIKE '/%' ORDER BY created_at",
            (self.owner,),
        )
        for job in jobs:
            text = json.loads(job["payload"])["text"]
            response = await self.command(job["conversation_id"], text)

            def complete(db: sqlite3.Connection, job: Json = job, response: str = response) -> None:
                Delivery(self.db, self.settings).prepare_in(
                    db,
                    job["conversation_id"],
                    "send_message",
                    {"text": response},
                    f"final:{job['id']}",
                    role="final",
                    durable_obligation=True,
                )
                db.execute(
                    "UPDATE jobs SET status='completed',outcome=?,updated_at=? WHERE id=? AND status='queued'",
                    (encode({"command": True}), self.db.clock(), job["id"]),
                )

            await self.db.write(complete)

    async def command(self, conversation: str, text: str) -> str:
        pieces = text.strip().split()
        command = pieces[0].split("@")[0]
        if command in ("/help", "/start"):
            return "Theo commands: /status /backend [name model] /models /jobs /cancel <job-id> /pause [background|models|notifications] /resume [scope] /memory [query] /goals /usage /help. Requested reminders remain active during background pause."
        if command == "/cancel" and len(pieces) == 2:
            await self.cancel(pieces[1])
            return "Cancellation recorded. Already dispatched effects remain inspectable."
        if command in ("/pause", "/resume"):
            scope = pieces[1] if len(pieces) > 1 else "background"
            if scope not in ("background", "models", "notifications"):
                return "Choose background, models or notifications."
            if command == "/resume" and scope == "background":
                from theo.qualification import qualification_status

                if not (await qualification_status(self.db, self.settings))["production_qualified"]:
                    return "Background activation requires recorded native, Mac, behaviour and seven-day deployment qualification."
            await self.db.set_control(
                self.owner, scope + "_paused", "true" if command == "/pause" else "false"
            )
            return f"{scope.capitalize()} {'paused' if command == '/pause' else 'resumed'}. Requested reminder schedules are preserved."
        if command == "/backend":
            if len(pieces) == 3:
                from theo.backends.policy import BACKENDS

                if pieces[1] not in BACKENDS:
                    return "Available adapters: claude, codex, cursor, grok."
                await self.db.execute(
                    "UPDATE conversations SET backend=?,model=? WHERE id=? AND owner_id=?",
                    (pieces[1], pieces[2], conversation, self.owner),
                )
                return "Route preference saved. Eligibility is verified before each run; canonical memory carries across."
            row = await self.db.one(
                "SELECT backend,model FROM conversations WHERE id=?", (conversation,)
            )
            return encode(row)
        if command == "/models":
            return encode(
                await self.db.read(
                    "SELECT backend,models,status,verified_at FROM backend_accounts WHERE owner_id=?",
                    (self.owner,),
                )
            )
        if command == "/usage":
            from theo.backends.policy import Accounts

            return encode(await Accounts(self.db, self.owner).usage())
        if command == "/jobs":
            return encode(
                await self.db.read(
                    "SELECT id,kind,status,updated_at FROM jobs WHERE owner_id=? ORDER BY created_at DESC LIMIT 20",
                    (self.owner,),
                )
            )
        if command == "/memory":
            from theo.memory import Memory

            return encode(await Memory(self.db, self.owner).search(" ".join(pieces[1:]), 10))
        if command == "/goals":
            return encode(
                await self.db.read(
                    "SELECT id,title,status,blocker FROM goals WHERE owner_id=?", (self.owner,)
                )
            )
        if command == "/status":
            return encode(await status(self.db, self.settings))
        return "Unknown command. Use /help."


async def status(db: Database, settings: Settings) -> Json:
    owner = settings.owner_id
    controls = await db.read(
        "SELECT key,value FROM control WHERE owner_id=? AND key IN ('background_paused','models_paused','notifications_paused','quarantined')",
        (owner,),
    )
    jobs = await db.read(
        "SELECT status,count(*) count FROM jobs WHERE owner_id=? GROUP BY status", (owner,)
    )
    unresolved = await db.one(
        "SELECT count(*) count FROM actions WHERE owner_id=? AND status='uncertain'", (owner,)
    )
    heartbeat = await db.one(
        "SELECT max(heartbeat_at) heartbeat FROM lifecycle_intervals WHERE owner_id=?", (owner,)
    )
    return {
        "name": settings.name,
        "core_healthy": bool(
            heartbeat and heartbeat["heartbeat"] and db.clock() - heartbeat["heartbeat"] < 60
        ),
        "controls": controls,
        "jobs": jobs,
        "uncertain_actions": unresolved["count"] if unresolved else 0,
        "native_execution": "requires verified included account and OS isolation",
        "memory_retrieval": "semantic_assets_present"
        if (db.root / "models/embeddings/manifest.json").exists()
        else "fts_only",
    }


async def serve(db: Database, settings: Settings, token: str | None = None) -> None:
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
    from theo.process_registry import terminate_registered

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
            from theo.delivery import NoEffect

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
            await coordinator.commands()
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
