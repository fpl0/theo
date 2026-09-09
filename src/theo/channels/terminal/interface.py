"""Interactive terminal prompt and live turn-following loop.

Owns keyboard bindings, attachment staging, slash-command presentation and Ctrl+C
behavior. Uses TerminalClient for durable state and presentation for rendering.
"""

import asyncio
import signal
import sys
from pathlib import Path
from types import FrameType

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import NestedCompleter, PathCompleter
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from theo.channels.terminal.attachments import MAX_ATTACHMENTS, extract_references, pasted_paths
from theo.channels.terminal.client import TerminalClient
from theo.channels.terminal.presentation import render_turn, safe_text
from theo.config import Settings
from theo.domain import Denied, TheoError, uid
from theo.storage import Database

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
                from theo.application.status import status

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
