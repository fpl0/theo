"""Terminal view data and safe Rich rendering for a single assistant turn.

Strips terminal control characters and renders draft text, tool progress and
completion state without reading the database or admitting new work.
"""

import re
from dataclasses import dataclass, field

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def safe_text(value: str) -> str:
    return CONTROL.sub("", value)


@dataclass
class TurnView:
    status: str
    preview: str = ""
    answer: str = ""
    tools: list[str] = field(default_factory=list[str])
    delivery: str = ""
    done: bool = False


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
