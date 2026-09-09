"""SQLite-backed terminal client for the running Theo daemon.

Resolves conversations and routes, submits durable jobs, reads turn state and
records cancellation. Prompt handling and Rich rendering belong to the interface.
"""

import asyncio
import json
import re
from pathlib import Path

from theo.backends.policy import BACKENDS
from theo.channels.terminal.attachments import attachment_parts
from theo.channels.terminal.presentation import TurnView
from theo.config import Settings
from theo.domain import Denied, Json, uid
from theo.observability import telemetry
from theo.storage import Database
from theo.work.jobs import Jobs


class TerminalClient:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self.owner = settings.owner_id
        self.conversation = ""
        self.session = "default"
        self.last_job: str | None = None
        self._observed_turn: tuple[str, float] | None = None
        self._first_visible = False

    @telemetry.observed("cli.connect", channel="cli")
    async def connect(self, name: str) -> None:
        if not re.fullmatch(r"[\w.-]{1,64}", name):
            raise ValueError(
                "Session names use 1–64 letters, numbers, dots, underscores or hyphens"
            )
        self.session = name
        self.conversation = await self.db.conversation(self.owner, "local", "terminal:" + name)
        row = await self.db.one(
            "SELECT id FROM jobs WHERE owner_id=? AND conversation_id=? ORDER BY created_at DESC LIMIT 1",
            (self.owner, self.conversation),
        )
        self.last_job = row["id"] if row else None

    @telemetry.observed("cli.ensure_running", channel="cli")
    async def ensure_running(self) -> None:
        row = await self.db.one(
            "SELECT max(heartbeat_at) t FROM lifecycle_intervals WHERE owner_id=? AND ended_at IS NULL",
            (self.owner,),
        )
        if not row or not row["t"] or self.db.clock() - row["t"] > 90:
            raise Denied(
                "Theo is not responding. Start `theo serve` for this data root, then reconnect."
            )

    async def route(self, backend: str | None = None, model: str | None = None) -> str:
        if backend is not None:
            if backend not in BACKENDS or not model or not model.strip():
                raise ValueError("Use /model BACKEND MODEL with an included model ID")
            await self.db.execute(
                "UPDATE conversations SET backend=?,model=? WHERE id=? AND owner_id=?",
                (backend, model, self.conversation, self.owner),
            )
        row = await self.db.one(
            "SELECT backend,model FROM conversations WHERE id=? AND owner_id=?",
            (self.conversation, self.owner),
        )
        assert row
        return f"{row['backend'] or self.settings.primary_backend or 'unconfigured'} / {row['model'] or self.settings.primary_model or 'select a model with /model'}"

    @telemetry.observed("cli.submit", channel="cli")
    async def submit(self, text: str, paths: list[Path]) -> str:
        await self.ensure_running()
        if not text.strip() and not paths:
            raise ValueError("Write a message or attach a file")
        pending = await self.db.one(
            "SELECT id FROM jobs WHERE owner_id=? AND conversation_id=? AND status IN ('queued','running','waiting_for_auth','waiting_for_quota','waiting_for_user','waiting_for_dependency','interrupted','uncertain') LIMIT 1",
            (self.owner, self.conversation),
        )
        if pending:
            raise Denied(
                "This conversation has unfinished work. Use /wait or /cancel before another message."
            )
        parts = await attachment_parts(self.db, self.settings, paths)
        job = await Jobs(self.db, self.owner).ingest(
            self.conversation,
            "local",
            uid(),
            {"source": "interactive_terminal"},
            text.strip() or "Please inspect the attached files.",
            parts,
            require_idle=True,
        )
        assert job
        self.last_job = job
        self._observed_turn = (job, asyncio.get_running_loop().time())
        self._first_visible = False
        return job

    async def view(self, job_id: str) -> TurnView:
        job = await self.db.one(
            "SELECT * FROM jobs WHERE id=? AND owner_id=? AND conversation_id=?",
            (job_id, self.owner, self.conversation),
        )
        if job is None:
            raise Denied("Job is not in this terminal conversation")
        run = await self.db.one(
            "SELECT id,output,error FROM runs WHERE job_id=? ORDER BY generation DESC LIMIT 1",
            (job_id,),
        )
        events = (
            await self.db.read(
                "SELECT kind,payload FROM run_events WHERE run_id=? ORDER BY sequence", (run["id"],)
            )
            if run
            else []
        )
        preview = "".join(
            str(json.loads(e["payload"]).get("text", ""))
            for e in events
            if e["kind"] == "text_delta"
        )
        tools = (
            await self.db.read(
                "SELECT source FROM messages WHERE run_id=? AND role='tool' ORDER BY sequence",
                (run["id"],),
            )
            if run
            else []
        )
        action = await self.db.one(
            "SELECT status,request,error FROM actions WHERE owner_id=? AND semantic_key=?",
            (self.owner, "final:" + job_id),
        )
        answer = str(run["output"] or run["error"] or "") if run else ""
        delivery = str(action["status"]) if action else ""
        if action:
            request = json.loads(action["request"])
            answer = str(request.get("text", request.get("caption", answer)))
            if request.get("artifact_id"):
                answer += "\n\nArtifact: " + str(request["artifact_id"])
        done = job["status"] not in ("queued", "running")
        if (
            job["status"] in ("completed", "failed")
            and action
            and delivery in ("ready", "executing", "prepared")
        ):
            done = False
        if self._observed_turn and self._observed_turn[0] == job_id:
            elapsed = asyncio.get_running_loop().time() - self._observed_turn[1]
            link = await self.db.one(
                "SELECT traceparent FROM telemetry_links WHERE kind='job' AND entity_id=?",
                (job_id,),
            )
            if (preview or answer) and not self._first_visible:
                self._first_visible = True
                with telemetry.operation(
                    "cli.first_visible", upstream=link["traceparent"] if link else "", channel="cli"
                ):
                    telemetry.measure(
                        "theo_cli_first_visible_duration", elapsed, histogram=True, channel="cli"
                    )
                    telemetry.event(
                        "cli.first_visible.measured",
                        duration_seconds=elapsed,
                        channel="cli",
                        job_id=job_id,
                    )
            if done:
                with telemetry.operation(
                    "cli.turn_complete", upstream=link["traceparent"] if link else "", channel="cli"
                ):
                    telemetry.measure(
                        "theo_cli_turn_duration", elapsed, histogram=True, channel="cli"
                    )
                    telemetry.event(
                        "cli.turn_complete.measured",
                        duration_seconds=elapsed,
                        channel="cli",
                        job_id=job_id,
                    )
                self._observed_turn = None
        return TurnView(
            str(job["status"]),
            preview[:100000],
            answer,
            [str(t["source"]).removeprefix("tool:") for t in tools],
            delivery,
            done,
        )

    @telemetry.observed("cli.cancel", channel="cli")
    async def cancel(self) -> None:
        if self.last_job:
            await Jobs(self.db, self.owner).cancel(self.last_job)

    async def history(self) -> list[Json]:
        return (
            await self.db.read(
                "SELECT role,content,parts FROM messages WHERE owner_id=? AND conversation_id=? AND role IN ('user','assistant') ORDER BY sequence DESC LIMIT 20",
                (self.owner, self.conversation),
            )
        )[::-1]
