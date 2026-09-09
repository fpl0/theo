"""Run leased jobs through canonical context, native reasoning and final delivery.

Coordinates one attempt, maintains worker heartbeats, records outcomes and fences
cancellation. Service startup and owner command interpretation live separately.
"""

import asyncio
import contextlib
import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

from theo.application.commands import ConversationCommands
from theo.backends.base import NativeBackend
from theo.backends.factory import backend_for
from theo.channels.telegram.adapter import Telegram
from theo.config import Settings
from theo.delivery.ledger import Delivery
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
from theo.memory.context import ContextAssembler
from theo.memory.embeddings import Embeddings
from theo.observability import telemetry
from theo.privacy import group_scope
from theo.storage import Database
from theo.tools.broker import ToolBroker
from theo.tools.registry import REGISTRY
from theo.work.autonomy import CADENCES
from theo.work.improvement import Critic
from theo.work.jobs import Jobs


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
        self.commands_handler = ConversationCommands(db, settings, self.cancel)
        self.jobs = Jobs(db, self.owner)
        self.factory: Callable[[str], NativeBackend] = factory or (
            lambda name: backend_for(name, db=db, settings=settings)
        )
        self.telegram = telegram
        self.embeddings = Embeddings(db, self.owner)
        self.active: dict[str, tuple[NativeBackend | None, str]] = {}
        self.run_tasks: dict[str, asyncio.Task[None]] = {}

    async def run_job(self, job: Json) -> None:
        link = await self.db.one(
            "SELECT traceparent FROM telemetry_links WHERE kind='job' AND entity_id=?", (job["id"],)
        )
        conversation = await self.db.one(
            "SELECT channel FROM conversations WHERE id=?", (job["conversation_id"],)
        )
        channel = "cli" if conversation and conversation["channel"] == "local" else "telegram"
        with telemetry.operation(
            "job.run",
            upstream=link["traceparent"] if link else "",
            channel=channel,
            job_id=job["id"],
            kind=job["kind"],
        ):
            telemetry.measure(
                "theo_queue_duration",
                max(0, self.db.clock() - job["created_at"]),
                histogram=True,
                channel=channel,
            )
            await self._run_job(job)
            result = await self.db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))
            telemetry.mark_outcome(result["status"] if result else "unknown")
            telemetry.measure(
                "theo_jobs", channel=channel, outcome=result["status"] if result else "unknown"
            )

    async def _run_job(self, job: Json) -> None:
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
        if telemetry.carrier():
            await self.db.execute(
                "INSERT OR REPLACE INTO telemetry_links VALUES(?,?,?,?)",
                ("run", run_id, telemetry.carrier(), self.db.clock()),
            )
        self.active[job["id"]] = (None, run_id)
        task = asyncio.current_task()
        if task:
            self.run_tasks[job["id"]] = task
        context: Json | None = None
        # Hydration and local model loading can outlive the initial lease too.
        heartbeat = asyncio.create_task(self._heartbeat(job))
        presentation = asyncio.create_task(self._presentation(job)) if self.telegram else None
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
                    parts = [
                        await self.telegram.hydrate(part, job["conversation_id"]) for part in parts
                    ]
                    payload["parts"] = parts

                    def hydrated(connection: sqlite3.Connection) -> None:
                        self.jobs.check(connection, job["id"], job["generation"])
                        connection.execute(
                            "UPDATE jobs SET payload=? WHERE id=?", (encode(payload), job["id"])
                        )
                        if payload.get("message_id"):
                            connection.execute(
                                "UPDATE messages SET parts=? WHERE id=? AND owner_id=?",
                                (encode(parts), payload["message_id"], self.owner),
                            )

                    await self.db.write(hydrated)
                text = str(payload.get("text", ""))
                if parts:
                    text += "\nINPUT PARTS\n" + encode(parts)
                semantic = None
                try:
                    if not await group_scope(self.db, job["conversation_id"]):
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
                    instructions=context.get("instructions", ""),
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
                outcome = ExecutionOutcome(
                    status=Outcome.FAILED, error="Native runtime produced no terminal event"
                )
                preview_remaining = 100000
                if self.telegram and conversation["channel"] == "telegram":
                    await self.telegram.preview(job)
                async for event in backend.events(request):
                    if (
                        self.telegram
                        and conversation["channel"] == "telegram"
                        and event.kind == "text_delta"
                    ):
                        await self.telegram.preview(job, str(event.payload.get("text", "")))
                    if event.kind == "terminal":
                        outcome = ExecutionOutcome.model_validate(event.payload)
                    # Preserve event type and bounded observable data; no hidden reasoning or raw secrets.
                    event_payload = (
                        event.payload
                        if event.kind != "text_delta"
                        else {"characters": len(str(event.payload.get("text", "")))}
                    )
                    # Local clients can tail bounded, visible answer text. Never
                    # persist reasoning or credentials; other channels keep counts.
                    if event.kind == "text_delta" and conversation["channel"] == "local":
                        preview = str(event.payload.get("text", ""))[:preview_remaining]
                        preview_remaining -= len(preview)
                        event_payload = {"text": preview, "preview": True}
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
            await self.db.execute(
                "UPDATE runs SET status=CASE WHEN EXISTS(SELECT 1 FROM jobs WHERE id=? AND status='cancelled') THEN 'cancelled' ELSE 'interrupted' END,ended_at=? WHERE id=? AND status='running'",
                (job["id"], self.db.clock(), run_id),
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
            if presentation:
                presentation.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await presentation
            if self.telegram:
                with contextlib.suppress(Exception):
                    await self.telegram.end_preview(job["id"])
            self.run_tasks.pop(job["id"], None)
            self.broker.revoke(run_id)
            if heartbeat:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError, Denied):
                    await heartbeat
            backend, _ = self.active.pop(job["id"], (None, ""))
            if backend:
                await backend.cancel()
            await self.db.execute("DELETE FROM worker_processes WHERE run_id=?", (run_id,))

    async def _presentation(self, job: Json) -> None:
        assert self.telegram
        while True:
            with contextlib.suppress(Exception):
                await self.telegram.preview(job)
            await asyncio.sleep(1)

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
                task = self.run_tasks.get(child)
                if task and task is not asyncio.current_task():
                    task.cancel()

    async def reconcile_cancellations(self) -> None:
        """Apply cancellations committed by a separate local operator process."""
        for job_id in list(self.active):
            row = await self.db.one(
                "SELECT status FROM jobs WHERE id=? AND owner_id=?", (job_id, self.owner)
            )
            active = self.active.get(job_id)
            if active and row and row["status"] == "cancelled":
                _, run_id = active
                self.broker.revoke(run_id)
                await self.db.execute(
                    "UPDATE runs SET status='cancelled',ended_at=? WHERE id=? AND status='running'",
                    (self.db.clock(), run_id),
                )
                task = self.run_tasks.get(job_id)
                if task:
                    task.cancel()

    async def commands(self) -> None:
        await self.commands_handler.process_pending()

    async def command(self, conversation: str, text: str) -> str:
        return await self.commands_handler.command(conversation, text)
