"""Parse local attachment references and copy bounded inputs into artifact storage.

Recognizes explicit file references and whole-path pastes, validates the complete
attachment set, and returns canonical input parts for a terminal submission.
"""

import asyncio
import re
import shlex
from pathlib import Path
from urllib.parse import unquote, urlsplit

from theo.config import Settings
from theo.content.artifacts import Artifacts
from theo.domain import Json
from theo.storage import Database

MAX_ATTACHMENTS = 8


REFERENCE = re.compile(r"""(?<!\S)@(?:"([^"]+)"|'([^']+)'|([^\s]+))""")


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
