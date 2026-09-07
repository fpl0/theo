"""Explicit local asset provisioning and bounded embedding repair."""

import asyncio
import importlib
import json
import os
import sqlite3
import struct
from typing import Any

from theo.domain import Json, Unavailable, digest
from theo.memory import Memory
from theo.operations import file_hash
from theo.storage import Database

MODEL = "BAAI/bge-base-en-v1.5"
PREPROCESSING = digest(
    {
        "normalization": "fastembed-default",
        "passage": "none",
        "query": "Represent this sentence for searching relevant passages: ",
    }
)


class Embeddings:
    def __init__(self, db: Database, owner: str):
        self.db, self.owner = db, owner
        self.cache = db.root / "models/embeddings"
        self._model: Any = None
        self._lock = asyncio.Lock()

    async def install(self) -> Json:
        # The asset download is needed; optional ecosystem telemetry is not.
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["DO_NOT_TRACK"] = "1"
        module: Any = importlib.import_module("fastembed")
        self.cache.mkdir(parents=True, exist_ok=True)
        model = await asyncio.to_thread(
            module.TextEmbedding, model_name=MODEL, cache_dir=str(self.cache), threads=2
        )
        files = {
            str(p.relative_to(self.cache)): file_hash(p)
            for p in self.cache.rglob("*")
            if p.is_file() and p.name != "manifest.json" and not p.name.endswith(".lock")
        }
        manifest = {
            "model": MODEL,
            "dimensions": 768,
            "preprocessing": PREPROCESSING,
            "files": files,
            "revision_hash": digest(files),
        }
        (self.cache / "manifest.json").write_text(json.dumps(manifest, indent=2))
        self._model = model
        return manifest

    def _load(self) -> Any:
        manifest_path = self.cache / "manifest.json"
        if not manifest_path.exists():
            raise Unavailable("Local embedding assets are not installed")
        manifest = json.loads(manifest_path.read_text())
        if self._model is None:
            for relative, checksum in manifest["files"].items():
                path = (self.cache / relative).resolve()
                if not path.is_relative_to(self.cache.resolve()) or file_hash(path) != checksum:
                    raise Unavailable("Local model asset checksum mismatch")
            module: Any = importlib.import_module("fastembed")
            self._model = module.TextEmbedding(
                model_name=MODEL, cache_dir=str(self.cache), local_files_only=True, threads=2
            )
        return self._model

    async def vector(self, text: str, query: bool = False) -> list[float]:
        async with self._lock:

            def embed() -> list[float]:
                model = self._load()
                iterator = model.query_embed(text) if query else model.embed([text])
                return [float(value) for value in next(iter(iterator))]

            return await asyncio.to_thread(embed)

    async def repair_one(self) -> bool:
        row = await self.db.one(
            "SELECT e.memory_id,e.revision,r.body FROM embedding_jobs e JOIN memory_records m ON m.id=e.memory_id JOIN memory_revisions r ON r.memory_id=e.memory_id AND r.version=e.revision WHERE m.owner_id=? AND m.status='active' AND m.revision=e.revision AND e.retry_at<=? LIMIT 1",
            (self.owner, self.db.clock()),
        )
        if row is None:
            return False
        try:
            vector = await self.vector(row["body"])
            manifest = json.loads((self.cache / "manifest.json").read_text())
            await Memory(self.db, self.owner).store_embedding(
                row["memory_id"], row["revision"], vector, manifest["revision_hash"], PREPROCESSING
            )
        except Exception as exc:
            await self.db.execute(
                "UPDATE embedding_jobs SET attempts=attempts+1,retry_at=? WHERE memory_id=? AND revision=?",
                (self.db.clock() + 3600, row["memory_id"], row["revision"]),
            )
            await self.db.health(self.owner, "retrieval_degraded", {"error": type(exc).__name__})
        return True

    async def search(self, query: str) -> list[Json]:
        vector = await self.vector(query, query=True)
        manifest = json.loads((self.cache / "manifest.json").read_text())

        def search() -> list[Json]:
            module: Any = importlib.import_module("sqlite_vec")
            connection = sqlite3.connect(f"file:{self.db.path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                connection.enable_load_extension(True)
                module.load(
                    connection
                )  # locked package only; no model/user-supplied extension path
                connection.enable_load_extension(False)
                rows = connection.execute(
                    "SELECT m.*,r.body,r.provenance,r.source,vec_distance_cosine(e.vector,?) distance FROM embeddings e JOIN memory_records m ON m.id=e.memory_id AND m.revision=e.revision JOIN memory_revisions r ON r.memory_id=m.id AND r.version=m.revision WHERE m.owner_id=? AND m.status='active' AND e.model=? AND e.dimensions=? AND e.preprocessing=? ORDER BY distance LIMIT 50",
                    (
                        struct.pack(f"<{len(vector)}f", *vector),
                        self.owner,
                        manifest["revision_hash"],
                        len(vector),
                        PREPROCESSING,
                    ),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                connection.close()

        return await asyncio.to_thread(search)
