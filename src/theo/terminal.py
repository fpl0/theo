"""Local terminal client for the running daemon; SQLite is the durable transport."""

import asyncio
import json
import re
import shlex
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from urllib.parse import unquote, urlsplit

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import NestedCompleter, PathCompleter
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from theo.artifacts import Artifacts
from theo.backends.policy import BACKENDS
from theo.config import Settings
from theo.domain import Denied, Json, TheoError, uid
from theo.jobs import Jobs
from theo.storage import Database

MAX_ATTACHMENTS = 8
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
REFERENCE = re.compile(r"""(?<!\S)@(?:"([^"]+)"|'([^']+)'|([^\s]+))""")
HELP = """**Chat** — Enter sends; Alt+Enter adds a line. Paste multiline text normally.

**Attach** — `/attach path` (Tab completes), drag/paste file paths, or write
`Explain @./notes.md` and `Compare @\"./image one.png\" @./image-two.png`.
Attachments are copied when sent. Images use the selected model's vision support.

- `/attachments` · show pending files
- `/clear-attachments` · remove pending files
- `/model BACKEND MODEL` · select an included model; `/model` shows the route
- `/history` · show recent messages
- `/new` · start a separate conversation with shared memory
- `/resume NAME` · reopen a named conversation
- `/wait` · follow the most recent job again
- `/cancel` · cancel the most recent job
- `/status` · daemon and job status
- `/quit` · leave Theo running

**Ctrl+C** cancels the current turn; at the prompt it clears your input.
**Ctrl+D** exits at the prompt. Live text is a draft until the final response arrives.
"""


def safe_text(value: str) -> str:
    return CONTROL.sub("", value)


def local_path(value: str) -> Path:
    if value.startswith("file://"):
        parsed = urlsplit(value)
        if parsed.netloc not in ("", "localhost"):
            raise ValueError("Only local file URLs can be attached")
        value = unquote(parsed.path)
    return Path(value).expanduser().resolve()


def pasted_paths(value: str) -> list[Path]:
    """Only interpret a whole existing path/list as a paste, never ordinary prose."""
    try:
        single = local_path(value.strip())
        if single.is_file():
            return [single]
        values = shlex.split(value)
        paths = [local_path(v) for v in values]
        return paths if paths and all(p.is_file() for p in paths) else []
    except ValueError, OSError:
        return []


def extract_references(value: str) -> tuple[str, list[Path]]:
    paths: list[Path] = []

    def replace(match: re.Match[str]) -> str:
        path = local_path(next(group for group in match.groups() if group is not None))
        paths.append(path)
        return f"[attached: {path.name}]"

    return REFERENCE.sub(replace, value), paths


async def attachment_parts(db: Database, settings: Settings, paths: list[Path]) -> list[Json]:
    unique = list(dict.fromkeys(p.expanduser().resolve() for p in paths))
    if len(unique) > MAX_ATTACHMENTS:
        raise ValueError(f"Attach at most {MAX_ATTACHMENTS} files per message")

    # Validate all inputs before registering any artifact. Never recurse into directories.
    def read_files() -> list[tuple[Path, bytes]]:
        files: list[tuple[Path, bytes]] = []
        total = 0
        for path in unique:
            if not path.is_file():
                raise ValueError(f"Not a regular file: {path}")
            with path.open("rb") as stream:
                raw = stream.read(settings.max_media_bytes + 1)
            total += len(raw)
            if total > settings.max_media_bytes:
                raise ValueError(
                    f"Attachments exceed the {settings.max_media_bytes // (1024 * 1024)} MiB total limit"
                )
            files.append((path, raw))
        return files

    files = await asyncio.to_thread(read_files)
    parts: list[Json] = []
    for path, raw in files:
        artifact = await Artifacts(db, settings).store(raw, path.name, "Owner terminal attachment")
        record = await db.one(
            "SELECT extracted_text FROM artifacts WHERE id=? AND owner_id=?",
            (artifact["id"], settings.owner_id),
        )
        image = path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        parts.append(
            {
                "kind": "photo" if image else "document",
                "artifact_id": artifact["id"],
                "text": record["extracted_text"][:50000]
                if record and record["extracted_text"]
                else (
                    "Image attached; inspect with vision before describing."
                    if image
                    else "Original file retained; text extraction unavailable."
                ),
                "metadata": {"name": path.name, "size": len(raw), "mime": artifact["mime"]},
            }
        )
    return parts


@dataclass
class TurnView:
    status: str
    preview: str = ""
    answer: str = ""
    tools: list[str] = field(default_factory=list[str])
    delivery: str = ""
    done: bool = False


class TerminalClient:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self.owner = settings.owner_id
        self.conversation = ""
        self.session = "default"
        self.last_job: str | None = None

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
        return TurnView(
            str(job["status"]),
            preview[:100000],
            answer,
            [str(t["source"]).removeprefix("tool:") for t in tools],
            delivery,
            done,
        )

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


def render_turn(view: TurnView) -> Panel:
    caption = view.status.replace("_", " ")
    if view.delivery:
        caption += " · delivery " + view.delivery
    if view.tools:
        caption += " · tools: " + ", ".join(view.tools[-4:])
    text = view.answer if view.done else view.preview
    return Panel(
        Group(
            Text(safe_text(caption), style="dim"),
            Markdown(safe_text(text), hyperlinks=False) if text else Text("Thinking…", style="dim"),
        ),
        title="Theo" if view.done else "Theo · live draft",
        border_style="cyan",
        padding=(1, 2),
    )


async def follow(client: TerminalClient, console: Console) -> None:
    if client.last_job is None:
        console.print("No job in this conversation yet.")
        return
    cancelled = asyncio.Event()
    previous = signal.getsignal(signal.SIGINT)

    def interrupt(signum: int, frame: FrameType | None) -> None:
        cancelled.set()

    signal.signal(signal.SIGINT, interrupt)
    try:
        with Live(console=console, refresh_per_second=4, transient=True) as live:
            while True:
                if cancelled.is_set():
                    await client.cancel()
                    console.print(
                        "Cancellation requested. Already completed effects remain recorded.",
                        style="yellow",
                    )
                    return
                view = await client.view(client.last_job)
                live.update(render_turn(view))
                if view.done:
                    break
                try:
                    await client.ensure_running()
                except Denied:
                    console.print(
                        "Theo stopped responding. Your job is saved; use /wait after it restarts.",
                        style="yellow",
                    )
                    return
                await asyncio.sleep(0.25)
        console.print(render_turn(view))
    finally:
        signal.signal(signal.SIGINT, previous)


async def interactive(
    db: Database,
    settings: Settings,
    name: str,
    backend: str | None,
    model: str | None,
    attachments: list[Path],
) -> None:
    if not sys.stdin.isatty():
        raise Denied(
            'Interactive chat requires a terminal. Use theo chat "message" to queue from a script.'
        )
    client = TerminalClient(db, settings)
    await client.ensure_running()
    await client.connect(name)
    if model and not backend:
        raise ValueError("Choose both --backend and --model")
    route = await client.route(backend, model)
    console = Console(highlight=False)
    console.print(
        Panel(
            Text(
                f"{route}\nConversation: {name}\nPaste a file path or ask anything. /help for commands.",
                style="cyan",
            ),
            title="Theo",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    for row in (await client.history())[-6:]:
        console.print(Text("You" if row["role"] == "user" else "Theo", style="bold cyan"))
        console.print(Markdown(safe_text(row["content"]), hyperlinks=False))
    if client.last_job and not (await client.view(client.last_job)).done:
        console.print("Unfinished turn found. Use /wait to follow it.", style="yellow")
    pending = list(attachments)
    keys = KeyBindings()

    def _newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    keys.add("escape", "enter")(_newline)

    prompt: PromptSession[str] = PromptSession(
        key_bindings=keys,
        completer=NestedCompleter.from_nested_dict(
            {
                "/attach": PathCompleter(expanduser=True),
                **{
                    c: None
                    for c in (
                        "/help",
                        "/attachments",
                        "/clear-attachments",
                        "/model",
                        "/history",
                        "/new",
                        "/resume",
                        "/wait",
                        "/cancel",
                        "/status",
                        "/quit",
                    )
                },
            }
        ),
        complete_while_typing=False,
    )
    while True:
        try:
            label = f"You [{len(pending)} attached] › " if pending else "You › "
            value = (await prompt.prompt_async(label)).strip()
            if value in ("/quit", "/exit"):
                break
            if value == "/help":
                console.print(Markdown(HELP))
                continue
            if value.startswith("/attach "):
                raw = value[len("/attach ") :].strip()
                found = pasted_paths(raw)
                if not found:
                    raise ValueError("File not found. Quote paths containing spaces.")
                if len(set(pending + found)) > MAX_ATTACHMENTS:
                    raise ValueError(f"Attach at most {MAX_ATTACHMENTS} files")
                pending = list(dict.fromkeys(pending + found))
                console.print(Text("Attached: " + ", ".join(p.name for p in found), style="green"))
                continue
            if value == "/attachments":
                console.print(Text("\n".join(str(p) for p in pending) or "No pending files."))
                continue
            if value == "/clear-attachments":
                pending.clear()
                console.print("Attachments cleared.")
                continue
            if value == "/model" or value.startswith("/model "):
                args = value.split(maxsplit=2)
                if len(args) not in (1, 3):
                    raise ValueError("Use /model BACKEND MODEL")
                route = await client.route(*(args[1:] if len(args) == 3 else []))
                console.print(Text(route, style="cyan"))
                continue
            if value == "/new" or value.startswith("/resume "):
                await client.connect(uid()[:8] if value == "/new" else value.split(maxsplit=1)[1])
                pending.clear()
                console.print(
                    Text(f"Conversation: {client.session} · {await client.route()}", style="cyan")
                )
                continue
            if value == "/history":
                for row in await client.history():
                    console.print(Text(row["role"], style="bold cyan"))
                    console.print(Markdown(safe_text(row["content"]), hyperlinks=False))
                continue
            if value == "/wait":
                await follow(client, console)
                continue
            if value == "/cancel":
                await client.cancel()
                console.print("Cancellation requested." if client.last_job else "No job to cancel.")
                continue
            if value == "/status":
                from theo.runtime import status

                console.print_json(data=await status(db, settings))
                continue
            found = pasted_paths(value)
            if found:
                if len(set(pending + found)) > MAX_ATTACHMENTS:
                    raise ValueError(f"Attach at most {MAX_ATTACHMENTS} files")
                pending = list(dict.fromkeys(pending + found))
                console.print(Text("Attached: " + ", ".join(p.name for p in found), style="green"))
                continue
            if value.startswith("/"):
                raise ValueError("Unknown command. Use /help.")
            if not value and not pending:
                continue
            text, inline = extract_references(value)
            await client.submit(text, pending + inline)
            pending.clear()
            console.print(Text("You", style="bold cyan"))
            console.print(Text(safe_text(text or "Inspect attached files.")))
            await follow(client, console)
        except EOFError:
            break
        except KeyboardInterrupt:
            continue
        except (TheoError, ValueError, OSError) as exc:
            console.print(Text(safe_text(str(exc)), style="red"))
    console.print("Disconnected. Theo is still running.", style="dim")
