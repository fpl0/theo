"""Validate, extract and persist immutable content-addressed artifacts.

Scopes workspace paths, retains original bytes and stores extracted text and
metadata in SQLite. Channel routing and remote uploads live outside this service.
"""

import asyncio
import hashlib
import io
import mimetypes
import os
import sqlite3
import zipfile
from pathlib import Path

from theo.config import Settings
from theo.domain import Denied, Json, uid
from theo.privacy import label_in
from theo.storage import Database


def scoped_path(workspace: Path, path: str) -> Path:
    base = workspace.resolve()
    candidate = (base / path).resolve()
    if not candidate.is_relative_to(base):
        raise Denied("Path is outside this job's workspace")
    return candidate


def validate_artifact(raw: bytes, name: str) -> tuple[str, str]:
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    text = ""
    suffix = Path(name).suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = 30_000_000
        with Image.open(io.BytesIO(raw)) as picture:
            actual_mime = Image.MIME.get(picture.format or "")
            if actual_mime and actual_mime != mime:
                raise ValueError("Image content does not match its declared file type")
            if picture.width * picture.height > 30_000_000:
                raise ValueError("Image exceeds decoder pixel limit")
            picture.verify()
    elif suffix == ".pdf":
        from pypdf import PdfReader

        pdf = PdfReader(io.BytesIO(raw), strict=True)
        if pdf.is_encrypted or len(pdf.pages) > 1000:
            raise ValueError("Encrypted or oversized PDF")
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    elif suffix in (".docx", ".xlsx", ".pptx", ".epub", ".zip"):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if (
                len(archive.infolist()) > 10000
                or sum(x.file_size for x in archive.infolist()) > 128 * 1024 * 1024
            ):
                raise ValueError("Archive exceeds decompression limit")
            for item in archive.infolist():
                if Path(item.filename).is_absolute() or ".." in Path(item.filename).parts:
                    raise ValueError("Unsafe archive member")
            if (
                suffix in (".docx", ".xlsx", ".pptx")
                and "[Content_Types].xml" not in archive.namelist()
            ):
                raise ValueError("Invalid Office document")
            if archive.testzip():
                raise ValueError("Corrupt archive")
            if suffix == ".docx":
                from xml.etree import ElementTree

                xml = ElementTree.fromstring(archive.read("word/document.xml"))
                text = " ".join(node.text or "" for node in xml.iter() if node.tag.endswith("}t"))
    elif mime.startswith("text/") or suffix in (".md", ".json", ".py", ".csv", ".toml", ".yaml"):
        text = raw.decode("utf-8")
    if not raw:
        raise ValueError("Empty artifacts cannot establish completion")
    return mime, text


class Artifacts:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings, self.owner = db, settings, settings.owner_id

    async def register(
        self, workspace: Path, path: str, description: str, run_id: str | None = None
    ) -> Json:
        candidate = scoped_path(workspace, path)
        if candidate.stat().st_size > 256 * 1024 * 1024:
            raise ValueError("Artifact exceeds the 256 MiB registration bound")
        raw = await asyncio.to_thread(candidate.read_bytes)
        return await self.store(raw, candidate.name, description, run_id)

    async def store(
        self,
        raw: bytes,
        name: str,
        description: str,
        run_id: str | None = None,
        *,
        preserve_unparsed: bool = False,
    ) -> Json:
        validated = True
        try:
            mime, text = await asyncio.wait_for(asyncio.to_thread(validate_artifact, raw, name), 20)
        except Exception:
            if not preserve_unparsed or not raw:
                raise
            mime, text, validated = "application/octet-stream", "", False
        content_hash = hashlib.sha256(raw).hexdigest()
        body: bytes | None = raw
        location: str | None = None
        if len(raw) > self.settings.inline_blob_limit:
            location = f"blobs/{content_hash[:2]}/{content_hash}"
            destination = self.db.root / location
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not destination.exists():
                temp = destination.with_suffix("." + uid())
                with temp.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                temp.chmod(0o600)
                temp.replace(destination)
            body = None
        artifact_id = uid()

        def insert(db: sqlite3.Connection) -> None:
            db.execute(
                "INSERT OR IGNORE INTO blobs VALUES(?,?,?,?,?,?)",
                (content_hash, len(raw), mime, body, location, "available"),
            )
            db.execute(
                "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    self.owner,
                    content_hash,
                    Path(name).name,
                    description,
                    text,
                    run_id,
                    int(validated),
                    self.db.clock(),
                ),
            )

            if run_id:
                conversation = db.execute(
                    "SELECT r.conversation_id FROM runs n JOIN jobs r ON r.id=n.job_id JOIN telegram_destinations d ON d.conversation_id=r.conversation_id WHERE n.id=? AND d.private=0",
                    (run_id,),
                ).fetchone()
                if conversation:
                    label_in(db, "artifact", artifact_id, conversation[0])

        await self.db.write(insert)
        return {
            "id": artifact_id,
            "hash": content_hash,
            "size": len(raw),
            "mime": mime,
            "validated": validated,
        }

    async def content(self, artifact_id: str) -> tuple[Json, bytes]:
        row = await self.db.one(
            "SELECT a.*,b.size,b.mime,b.body,b.location,b.status FROM artifacts a JOIN blobs b ON b.hash=a.hash WHERE a.id=? AND a.owner_id=?",
            (artifact_id, self.owner),
        )
        if row is None or row["status"] != "available":
            raise Denied("Artifact unavailable")
        if row["body"] is not None:
            raw = bytes(row["body"])
        else:
            path = scoped_path(self.db.root, str(row["location"]))
            try:
                raw = await asyncio.to_thread(path.read_bytes)
            except FileNotFoundError:
                await self.db.execute(
                    "UPDATE blobs SET status='missing' WHERE hash=?", (row["hash"],)
                )
                raise Denied("Artifact bytes are missing") from None
        if hashlib.sha256(raw).hexdigest() != row["hash"]:
            raise Denied("Artifact checksum mismatch")
        return row, raw
