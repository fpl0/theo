"""One strict schema registry and run-bound domain facade for every backend."""

import asyncio
import json
import secrets
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from theo.artifacts import Artifacts, scoped_path
from theo.backends.policy import Accounts
from theo.browse import browse, render_public_page
from theo.config import Settings
from theo.delivery import Delivery
from theo.domain import (
    Denied,
    Json,
    StrictModel,
    TheoError,
    ToolContext,
    ToolResult,
    digest,
    encode,
    uid,
)
from theo.goals import Goals
from theo.jobs import Jobs
from theo.memory import Memory
from theo.scheduling import Scheduler
from theo.storage import Database, Transaction


class Empty(StrictModel):
    pass


class MessageArgs(StrictModel):
    text: str = Field(min_length=1, max_length=100000)
    reply_to: int | None = None
    target: str | None = None
    role: Literal["final", "progress"] = "progress"


class MessageIdArgs(StrictModel):
    message_id: int
    target: str | None = None


class EditArgs(MessageIdArgs):
    text: str = Field(min_length=1, max_length=4096)


class ForwardArgs(MessageIdArgs):
    from_chat_id: int


class MediaArgs(StrictModel):
    artifact_id: str
    caption: str | None = None
    reply_to: int | None = None
    target: str | None = None


class LocationArgs(StrictModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    target: str | None = None


class PollArgs(StrictModel):
    question: str = Field(min_length=1, max_length=300)
    options: list[str] = Field(min_length=2, max_length=12)
    target: str | None = None


class Button(StrictModel):
    text: str = Field(min_length=1, max_length=64)
    url: str


class ButtonsArgs(MessageArgs):
    buttons: list[Button] = Field(min_length=1, max_length=8)


class ReactionArgs(MessageIdArgs):
    emoji: str


class ScheduleArgs(StrictModel):
    text: str = Field(min_length=1)
    due_at: float | None = None
    cron: str | None = None
    interval_seconds: int | None = None
    timezone: str = "Europe/Dublin"


class IdArgs(StrictModel):
    id: str


class RememberArgs(StrictModel):
    body: str = Field(min_length=1)
    kind: Literal["entity", "episode", "procedure", "insight", "preference"] = "episode"
    source_message_id: str | None = None
    target_memory_id: str | None = None
    expected_revision: int | None = None


class RecallArgs(StrictModel):
    query: str
    limit: int = Field(default=20, ge=1, le=50)


class ConversationArgs(StrictModel):
    limit: int = Field(default=30, ge=1, le=100)


class ConnectArgs(StrictModel):
    source_id: str
    target_id: str
    relation: str


class RestoreArgs(IdArgs):
    revision: int | None = Field(default=None, ge=1)


class BulkArgs(StrictModel):
    memories: list[RememberArgs] = Field(min_length=1, max_length=30)


class AttentionArgs(StrictModel):
    body: str = Field(min_length=1, max_length=4000)
    expires_at: float | None = None


class QualityArgs(StrictModel):
    rating: int = Field(ge=1, le=5)
    rationale: str


class BrowseArgs(StrictModel):
    url: str
    screenshot: bool = False


class DelegateArgs(StrictModel):
    task: str = Field(min_length=1, max_length=50000)
    deadline_seconds: int = Field(default=1800, ge=60, le=5400)


class GoalStep(StrictModel):
    title: str
    next_action: str
    capabilities: list[str] = Field(default_factory=list)


class GoalArgs(StrictModel):
    title: str
    criteria: str
    steps: list[GoalStep] = Field(default_factory=lambda: list[GoalStep]())


class GoalUpdateArgs(IdArgs):
    status: Literal["proposed", "active", "blocked", "paused", "completed", "abandoned"]
    evidence: str | None = None
    blocker: str | None = None


class StepArgs(IdArgs):
    evidence: str


class FactArgs(StrictModel):
    subject: str
    predicate: str
    value: str
    source_message_id: str
    expected_revision: int = 0


class ArtifactArgs(StrictModel):
    path: str
    description: str


class FileReadArgs(StrictModel):
    path: str


class FileWriteArgs(FileReadArgs):
    content: str = Field(max_length=1000000)


class CommandArgs(StrictModel):
    argv: list[str] = Field(min_length=1, max_length=100)
    timeout_seconds: int = Field(default=60, ge=1, le=300)


class VoiceArgs(StrictModel):
    text: str = Field(min_length=1, max_length=10000)
    voice: str | None = None


class SkillArgs(StrictModel):
    name: str
    body: str
    triggers: list[str] = Field(min_length=1, max_length=20)


REGISTRY: dict[str, tuple[type[StrictModel], str]] = {
    "send_message": (MessageArgs, "Queue an owner message; committed means queued, never sent."),
    "reply": (MessageArgs, "Queue a reply retaining its message reference."),
    "forward": (ForwardArgs, "Forward an existing Telegram message through the action ledger."),
    "edit_message": (EditArgs, "Edit an exact Telegram message."),
    "delete_message": (MessageIdArgs, "Request reviewed deletion of a Telegram message."),
    "pin": (MessageIdArgs, "Pin an existing Telegram message."),
    "send_photo": (MediaArgs, "Send a registered, validated photo."),
    "send_document": (MediaArgs, "Deliver a registered artifact."),
    "send_voice": (MediaArgs, "Deliver an existing local voice artifact."),
    "send_video": (MediaArgs, "Deliver a registered video."),
    "send_location": (LocationArgs, "Deliver geographic coordinates."),
    "send_poll": (PollArgs, "Create a Telegram poll."),
    "send_buttons": (ButtonsArgs, "Send URL buttons; approval callbacks are host-owned."),
    "react": (ReactionArgs, "React to a specific message."),
    "get_reactions": (MessageIdArgs, "Read reactions observed by the bot; absence is unknown."),
    "schedule_task": (ScheduleArgs, "Persist a reminder before promising it."),
    "list_tasks": (Empty, "List persisted schedules."),
    "delete_task": (IdArgs, "Cancel a schedule without deleting its history."),
    "remember": (
        RememberArgs,
        "Save an inference or propose a reviewed correction; no silent overwrite.",
    ),
    "recall": (RecallArgs, "Search current active SQLite memory."),
    "forget": (IdArgs, "Archive a memory with recoverable history."),
    "recall_conversation": (ConversationArgs, "Read canonical messages in this conversation."),
    "connect": (ConnectArgs, "Link memories with typed evidence."),
    "restore": (RestoreArgs, "Restore an archived memory or prior revision."),
    "bulk_memory": (BulkArgs, "Store a bounded batch with individual results."),
    "memory_history": (IdArgs, "Read complete immutable revisions."),
    "review_corrections": (
        Empty,
        "List correction proposals for owner review; the model cannot approve.",
    ),
    "pin_attention": (AttentionArgs, "Persist a contextual attention pin."),
    "unpin_attention": (IdArgs, "Remove a contextual attention pin."),
    "get_cost_report": (Empty, "Inspect nullable token usage and included allowance pool state."),
    "log_deep_work_quality": (
        QualityArgs,
        "Record a subjective rating alongside host-observed run outcomes.",
    ),
    "browse": (BrowseArgs, "Read a public web source as untrusted evidence."),
    "delegate": (DelegateArgs, "Create a durable child job with a final-report obligation."),
    "goal_create": (GoalArgs, "Create a structured outcome and executable plan."),
    "goal_update": (GoalUpdateArgs, "Transition a goal with evidence and dependency checks."),
    "step_complete": (StepArgs, "Complete one plan step with outcome evidence."),
    "fact_propose": (FactArgs, "Propose a fact revision for explicit owner review."),
    "artifact_register": (ArtifactArgs, "Validate and hash an actual workspace file."),
    "action_status": (IdArgs, "Inspect committed, pending, delivered or uncertain action state."),
    "file_read": (FileReadArgs, "Read a bounded text file inside this job's workspace."),
    "file_write": (FileWriteArgs, "Write a draft inside this job's isolated workspace."),
    "command_run": (
        CommandArgs,
        "Execute an argument array within the verified OS boundary and workspace.",
    ),
    "voice_create": (VoiceArgs, "Create a voice artifact using local macOS speech and FFmpeg."),
    "skill_propose": (
        SkillArgs,
        "Propose a versioned skill without activating it or expanding grants.",
    ),
}

BASELINE = tuple(REGISTRY)[:33]
OUTBOUND = set(tuple(REGISTRY)[:14])


class BoundDatabase(Database):
    """Every mutation rechecks the lease inside the same SQLite transaction."""

    def __init__(self, parent: Database, context: ToolContext):
        self.parent, self.context = parent, context
        self.root, self.path, self.clock = parent.root, parent.path, parent.clock

    async def write[T](self, fn: Transaction[T]) -> T:
        def guarded(db: sqlite3.Connection) -> T:
            Jobs(self.parent, self.context.owner_id).check(
                db, self.context.job_id, self.context.generation
            )
            return fn(db)

        return await self.parent.write(guarded)

    async def read(self, sql: str, args: Sequence[Any] = ()) -> list[Json]:
        await self.write(lambda db: None)
        return await self.parent.read(sql, args)


class ToolBroker:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self.tokens: dict[str, ToolContext] = {}
        self.calls: dict[str, set[asyncio.Task[Any]]] = {}
        self.server: asyncio.Server | None = None

    def grant(self, context: ToolContext) -> str:
        token = secrets.token_urlsafe(32)
        self.tokens[token] = context
        return token

    def revoke(self, run_id: str) -> None:
        for task in self.calls.pop(run_id, set()):
            task.cancel()
        self.tokens = {
            token: context for token, context in self.tokens.items() if context.run_id != run_id
        }

    def definitions(self, context: ToolContext) -> list[Json]:
        return [
            {"name": name, "description": description, "inputSchema": schema.model_json_schema()}
            for name, (schema, description) in REGISTRY.items()
            if name in context.tools
        ]

    async def call(self, token: str, name: str, arguments: Json) -> ToolResult:
        context = self.tokens.get(token)
        if context is None or name not in context.tools or name not in REGISTRY:
            return ToolResult(status="denied", error="Tool grant unavailable")
        db = BoundDatabase(self.db, context)
        current = asyncio.current_task()
        if current:
            self.calls.setdefault(context.run_id, set()).add(current)
        try:
            await db.write(lambda connection: None)
            args = REGISTRY[name][0].model_validate(arguments).model_dump(exclude_none=True)
            read_only = {
                "recall",
                "memory_history",
                "review_corrections",
                "recall_conversation",
                "list_tasks",
                "get_cost_report",
                "get_reactions",
                "action_status",
                "file_read",
            }
            receipt_key = digest({"tool": name, "arguments": args})
            if name not in read_only and name not in OUTBOUND:

                def reserve(connection: sqlite3.Connection) -> Json | None:
                    old = connection.execute(
                        "SELECT result FROM tool_receipts WHERE owner_id=? AND job_id=? AND semantic_key=?",
                        (context.owner_id, context.job_id, receipt_key),
                    ).fetchone()
                    if old:
                        return json.loads(old[0])
                    uncertain = ToolResult(
                        status="uncertain",
                        error="The prior tool attempt has no committed receipt; inspect its effects before a new job retries it.",
                    )
                    connection.execute(
                        "INSERT INTO tool_receipts VALUES(?,?,?,?,?)",
                        (
                            context.owner_id,
                            context.job_id,
                            receipt_key,
                            uncertain.model_dump_json(),
                            db.clock(),
                        ),
                    )
                    return None

                cached = await db.write(reserve)
                if cached is not None:
                    return ToolResult.model_validate(cached)
            result = await self._dispatch(db, context, name, args)
            if name not in read_only and name not in OUTBOUND:
                await db.execute(
                    "UPDATE tool_receipts SET result=? WHERE owner_id=? AND job_id=? AND semantic_key=?",
                    (result.model_dump_json(), context.owner_id, context.job_id, receipt_key),
                )
            await db.message(
                context.owner_id,
                context.conversation_id,
                "tool",
                encode({"tool": name, "result": result.model_dump(mode="json")})[:100000],
                run_id=context.run_id,
                source=f"tool:{name}",
            )
            return result
        except ValidationError:
            return ToolResult(status="invalid", error="Arguments do not match the tool schema")
        except TheoError as exc:
            return ToolResult(status=exc.code, error=str(exc), retryable=exc.retryable)
        except (ValueError, OSError, sqlite3.Error) as exc:
            return ToolResult(
                status="failed",
                error=f"Tool rejected input or could not commit: {type(exc).__name__}",
            )
        finally:
            if current:
                self.calls.get(context.run_id, set()).discard(current)

    async def _dispatch(
        self, db: BoundDatabase, ctx: ToolContext, name: str, args: Json
    ) -> ToolResult:
        owner = ctx.owner_id
        memory = Memory(db, owner)
        source = f"run:{ctx.run_id}"
        data: Any = None
        if name in OUTBOUND:
            if "artifact_id" in args:
                await Artifacts(db, self.settings).content(args["artifact_id"])
            if name == "send_buttons":
                from theo.browse import validate_url

                for button in args["buttons"]:
                    validate_url(button["url"])
            role = args.pop("role", "progress")
            target = args.pop("target", None)
            key = (
                f"final:{ctx.job_id}"
                if role == "final"
                else f"tool:{ctx.job_id}:{name}:{digest(args)}"
            )
            job = await db.one(
                "SELECT lane,kind FROM jobs WHERE id=? AND owner_id=?", (ctx.job_id, owner)
            )
            action = await Delivery(db, self.settings).prepare(
                ctx.conversation_id,
                name,
                args,
                key,
                job_id=ctx.job_id,
                run_id=ctx.run_id,
                generation=ctx.generation,
                role=role,
                autonomous=bool(job and job["lane"] == "background"),
                discretionary=bool(
                    job
                    and job["lane"] == "background"
                    and not (role == "final" and job["kind"] in ("delegated", "deep_work"))
                ),
                target=target,
                require_approval=name in ("delete_message", "forward"),
            )
            row = await db.one("SELECT status FROM actions WHERE id=?", (action,))
            return ToolResult(status=str(row["status"]) if row else "failed", action_id=action)
        if name == "get_reactions":
            data = await db.read(
                "SELECT body,created_at FROM feedback WHERE owner_id=? AND kind='reaction' AND json_extract(body,'$.message_id')=?",
                (owner, args["message_id"]),
            )
            return ToolResult(status="ok", data={"observed": data, "complete": False})
        if name == "remember":
            source_message = args.get("source_message_id")
            if source_message:
                row = await db.one(
                    "SELECT content FROM messages WHERE id=? AND owner_id=? AND conversation_id=? AND role='user'",
                    (source_message, owner, ctx.conversation_id),
                )
                if row is None:
                    raise Denied("Source must be an actual owner message in this conversation")
                source = f"message:{source_message}"
            if args.get("target_memory_id"):
                if args.get("expected_revision") is None:
                    raise ValueError("Correction requires expected_revision")
                data = {
                    "correction_id": await memory.propose(
                        args["target_memory_id"], args["expected_revision"], args["body"], source
                    )
                }
                return ToolResult(status="pending_review", data=data)
            data = {"id": await memory.remember(args["body"], kind=args["kind"], source=source)}
        elif name == "recall":
            data = await memory.search(args["query"], args["limit"])
        elif name == "forget":
            await memory.archive(args["id"])
        elif name == "restore":
            data = {"revision": await memory.restore(args["id"], args.get("revision"))}
        elif name == "memory_history":
            data = await memory.history(args["id"])
        elif name == "review_corrections":
            data = await db.read(
                "SELECT * FROM corrections WHERE owner_id=? AND status='proposed'", (owner,)
            )
        elif name == "connect":
            data = {
                "id": await memory.connect(
                    args["source_id"], args["target_id"], args["relation"], source
                )
            }
        elif name == "bulk_memory":
            data = [
                (await self._dispatch(db, ctx, "remember", item)).model_dump(mode="json")
                for item in args["memories"]
            ]
        elif name == "recall_conversation":
            data = await db.read(
                "SELECT id,role,content,parts,created_at FROM messages WHERE owner_id=? AND conversation_id=? ORDER BY sequence DESC LIMIT ?",
                (owner, ctx.conversation_id, args["limit"]),
            )
        elif name == "schedule_task":
            data = {
                "id": await Scheduler(db, owner).create(
                    ctx.conversation_id,
                    args["text"],
                    due=args.get("due_at"),
                    cron=args.get("cron"),
                    interval=args.get("interval_seconds"),
                    timezone=args["timezone"],
                )
            }
        elif name == "list_tasks":
            data = await db.read(
                "SELECT * FROM schedules WHERE owner_id=? ORDER BY next_due", (owner,)
            )
        elif name == "delete_task":
            await Scheduler(db, owner).cancel(args["id"])
        elif name == "pin_attention":
            data = {"id": uid()}
            await db.execute(
                "INSERT INTO attention_pins VALUES(?,?,?,?,?,?)",
                (data["id"], owner, args["body"], source, args.get("expires_at"), db.clock()),
            )
        elif name == "unpin_attention":
            await db.execute(
                "DELETE FROM attention_pins WHERE id=? AND owner_id=?", (args["id"], owner)
            )
        elif name == "get_cost_report":
            data = await Accounts(db, owner).usage()
        elif name == "log_deep_work_quality":
            artifacts = await db.one(
                "SELECT count(*) n FROM artifacts WHERE owner_id=? AND run_id=? AND validated=1",
                (owner, ctx.run_id),
            )
            delivered = await db.one(
                "SELECT count(*) n FROM actions WHERE owner_id=? AND run_id=? AND status='succeeded'",
                (owner, ctx.run_id),
            )
            data = {
                "rating": args["rating"],
                "rationale": args["rationale"],
                "artifact_count": artifacts["n"] if artifacts else 0,
                "delivered_count": delivered["n"] if delivered else 0,
            }
            await db.execute(
                "INSERT INTO feedback VALUES(?,?,?,?,?,?,?,?)",
                (uid(), owner, ctx.run_id, None, "quality", encode(data), 0, db.clock()),
            )
        elif name == "browse":
            data = await browse(args["url"])
            if args["screenshot"]:
                raw = await render_public_page(args["url"])
                data["screenshot"] = await Artifacts(db, self.settings).store(
                    raw, "page.png", args["url"], ctx.run_id
                )
        elif name == "delegate":
            data = {
                "job_id": await Jobs(db, owner).enqueue(
                    ctx.conversation_id,
                    "delegated",
                    {"text": args["task"]},
                    f"delegate:{ctx.job_id}:{digest(args)}",
                    parent=ctx.job_id,
                    deadline=db.clock() + args["deadline_seconds"],
                )
            }
        elif name == "goal_create":
            data = {
                "id": await Goals(db, owner).create(
                    args["title"], args["criteria"], ctx.conversation_id, args["steps"]
                )
            }
        elif name == "goal_update":
            await Goals(db, owner).update(
                args["id"],
                args["status"],
                evidence=args.get("evidence"),
                blocker=args.get("blocker"),
            )
        elif name == "step_complete":
            await Goals(db, owner).complete_step(args["id"], args["evidence"])
        elif name == "fact_propose":
            row = await db.one(
                "SELECT id FROM messages WHERE id=? AND owner_id=? AND role='user'",
                (args["source_message_id"], owner),
            )
            if row is None:
                raise Denied("Fact proposal requires an owner source")
            proposal = uid()
            await db.execute(
                "INSERT OR IGNORE INTO proposals VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    proposal,
                    owner,
                    "fact",
                    digest(args),
                    args["source_message_id"],
                    encode(args),
                    "proposed",
                    None,
                    db.clock(),
                ),
            )
            return ToolResult(status="pending_review", data={"proposal_id": proposal})
        elif name == "artifact_register":
            data = await Artifacts(db, self.settings).register(
                ctx.workspace, args["path"], args["description"], ctx.run_id
            )
        elif name == "action_status":
            data = await db.one(
                "SELECT id,operation,status,receipt,error FROM actions WHERE id=? AND owner_id=?",
                (args["id"], owner),
            )
            if data is None:
                raise Denied("Action unavailable")
        elif name == "file_read":
            path = scoped_path(ctx.workspace, args["path"])
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("File exceeds text read limit")
            data = {"path": args["path"], "content": await asyncio.to_thread(path.read_text)}
        elif name == "file_write":
            path = scoped_path(ctx.workspace, args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_text, args["content"])
            data = {"path": args["path"], "bytes": len(args["content"].encode())}
        elif name == "command_run":
            from theo.workspaces import execute_scoped

            data = await execute_scoped(
                self.settings, db.root, ctx.workspace, args["argv"], args["timeout_seconds"]
            )
            if data["exit_code"] != 0:
                return ToolResult(status="failed", data=data, error="Command exited unsuccessfully")
        elif name == "voice_create":
            from theo.media import speak

            path = ctx.workspace / ("voice-" + uid() + ".ogg")
            await speak(args["text"], path, args.get("voice"))
            data = await Artifacts(db, self.settings).register(
                ctx.workspace, path.name, "Local voice response", ctx.run_id
            )
        elif name == "skill_propose":
            from theo.improvement import Improvement

            skill = await Improvement(db, owner).propose_skill(
                args["name"], args["body"], args["triggers"], source
            )
            return ToolResult(status="pending_review", data={"skill_id": skill})
        else:
            raise Denied("Capability unavailable")
        return ToolResult(
            status="committed"
            if name
            not in (
                "recall",
                "memory_history",
                "recall_conversation",
                "list_tasks",
                "get_cost_report",
                "browse",
                "action_status",
                "file_read",
                "review_corrections",
            )
            else "ok",
            data=data,
        )

    async def listen(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._handle, str(path), limit=1024 * 1024)
        path.chmod(0o600)
        if self.settings.runner_gid is not None:
            import os

            os.chown(path.parent, -1, self.settings.runner_gid)
            os.chown(path, -1, self.settings.runner_gid)
            path.parent.chmod(0o710)
            path.chmod(0o660)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while raw := await asyncio.wait_for(reader.readline(), 60):
                packet: Json = json.loads(raw)
                token = packet.get("token", "")
                context = self.tokens.get(token)
                if context is None:
                    response: Json = {"status": "denied", "error": "Run grant unavailable"}
                elif packet.get("method") == "list":
                    response = {"tools": self.definitions(context)}
                else:
                    response = (
                        await self.call(token, packet.get("name", ""), packet.get("arguments", {}))
                    ).model_dump(mode="json")
                writer.write((encode(response) + "\n").encode())
                await writer.drain()
        except TimeoutError, ValueError, ConnectionError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def close(self) -> None:
        for run_id in list(self.calls):
            self.revoke(run_id)
        self.tokens.clear()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
