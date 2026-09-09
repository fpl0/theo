"""Render a small allowlist of Markdown into Telegram-safe HTML.

Escapes raw markup and leaves unsupported syntax literal; Bot API rejection
fallback and message chunking are handled by sender and delivery respectively.
"""

import html
import re


def rich_html(text: str) -> str:
    def inline(line: str) -> str:
        escaped = html.escape(line)
        escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
        escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", escaped)
        escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)
        return re.sub(r"\[([^\]\n]+)\]\((https?://[^\s<>\)]+)\)", r'<a href="\2">\1</a>', escaped)

    blocks: list[str] = []
    code: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("```"):
            if code is None:
                code = []
            else:
                blocks.append("<pre>" + html.escape("\n".join(code)) + "</pre>")
                code = None
        elif code is not None:
            code.append(line)
        elif line.startswith("> "):
            blocks.append("<blockquote>" + inline(line[2:]) + "</blockquote>")
        elif re.match(r"^#{1,6} ", line):
            blocks.append("<b>" + inline(line.lstrip("# ")) + "</b>")
        else:
            blocks.append(inline(line))
    if code is not None:
        blocks.append("<pre>" + html.escape("\n".join(code)) + "</pre>")
    # Rich HTML collapses raw newlines, unlike Bot API parse_mode HTML.
    return "<br>".join(blocks)
