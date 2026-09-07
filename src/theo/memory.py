"""Revisioned memory, explicit correction review and current-only retrieval."""

import math
import re
import sqlite3
import struct

from theo.domain import Conflict, Denied, Json, encode, uid
from theo.storage import Database


class Memory:
    def __init__(self, db: Database, owner: str):
        self.db, self.owner = db, owner

    def _get(self, db: sqlite3.Connection, memory_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT m.*,r.body,r.provenance,r.source,r.metadata FROM memory_records m JOIN memory_revisions r ON r.memory_id=m.id AND r.version=m.revision WHERE m.id=? AND m.owner_id=?",
            (memory_id, self.owner),
        ).fetchone()
        if row is None:
            raise Denied("Memory unavailable")
        return row

    def index_in(
        self, db: sqlite3.Connection, memory_id: str, revision: int, body: str, active: bool = True
    ) -> None:
        db.execute(
            "DELETE FROM memory_fts WHERE memory_id=? AND owner_id=?", (memory_id, self.owner)
        )
        if active:
            db.execute("INSERT INTO memory_fts VALUES(?,?,?)", (memory_id, self.owner, body))
            db.execute(
                "INSERT INTO embedding_jobs(memory_id,revision) VALUES(?,?) ON CONFLICT(memory_id) DO UPDATE SET revision=excluded.revision,status='queued',attempts=0,retry_at=0",
                (memory_id, revision),
            )

    async def remember(
        self,
        body: str,
        *,
        kind: str = "episode",
        provenance: str = "inference",
        source: str,
        importance: float = 0.5,
        pinned: bool = False,
        metadata: Json | None = None,
    ) -> str:
        if not body.strip() or not source or not 0 <= importance <= 1:
            raise ValueError("Memory requires body, source and valid importance")
        memory_id = uid()

        def insert(db: sqlite3.Connection) -> str:
            timestamp = self.db.clock()
            db.execute(
                "INSERT INTO memory_records(id,owner_id,kind,revision,status,importance,pinned,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    memory_id,
                    self.owner,
                    kind,
                    1,
                    "active",
                    importance,
                    int(pinned),
                    timestamp,
                    timestamp,
                ),
            )
            db.execute(
                "INSERT INTO memory_revisions(memory_id,version,body,provenance,source,metadata,created_at) VALUES(?,?,?,?,?,?,?)",
                (memory_id, 1, body, provenance, source, encode(metadata or {}), timestamp),
            )
            self.index_in(db, memory_id, 1, body)
            return memory_id

        return await self.db.write(insert)

    async def show(self, memory_id: str) -> Json:
        return await self.db.write(lambda db: dict(self._get(db, memory_id)))

    async def history(self, memory_id: str) -> list[Json]:
        await self.show(memory_id)
        return await self.db.read(
            "SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY version", (memory_id,)
        )

    def _edit(
        self,
        db: sqlite3.Connection,
        memory_id: str,
        expected: int,
        body: str,
        source: str,
        provenance: str,
    ) -> int:
        row = self._get(db, memory_id)
        if row["revision"] != expected:
            raise Conflict(f"Current revision is {row['revision']}")
        if not body.strip():
            raise ValueError("Memory body cannot be empty")
        version = expected + 1
        timestamp = self.db.clock()
        db.execute(
            "INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?)",
            (memory_id, version, body, provenance, source, row["metadata"], timestamp),
        )
        db.execute(
            "UPDATE memory_records SET revision=?,updated_at=? WHERE id=?",
            (version, timestamp, memory_id),
        )
        self.index_in(db, memory_id, version, body, row["status"] == "active")
        self._invalidate(db)
        return version

    def _invalidate(self, db: sqlite3.Connection) -> None:
        # Conservative invalidation prevents an old draft from reintroducing corrected state.
        db.execute("UPDATE context_snapshots SET invalidated=1 WHERE owner_id=?", (self.owner,))
        db.execute("UPDATE sessions SET valid=0 WHERE owner_id=?", (self.owner,))
        db.execute(
            "UPDATE actions SET status='cancelled',error='canonical_state_changed' WHERE owner_id=? AND status IN ('ready','awaiting_approval') AND scope='draft'",
            (self.owner,),
        )
        db.execute(
            "UPDATE outbox SET status='cancelled' WHERE action_id IN (SELECT id FROM actions WHERE owner_id=? AND status='cancelled') AND status='ready'",
            (self.owner,),
        )

    async def edit(
        self, memory_id: str, expected: int, body: str, *, source: str, actor: str = "owner"
    ) -> int:
        if actor != "owner":
            raise Denied("Model changes must enter correction review")
        return await self.db.write(
            lambda db: self._edit(db, memory_id, expected, body, source, "owner")
        )

    async def propose(self, memory_id: str, expected: int, body: str, source: str) -> str:
        proposal_id = uid()

        def insert(db: sqlite3.Connection) -> str:
            self._get(db, memory_id)
            if not body.strip():
                raise ValueError("Proposed body cannot be empty")
            db.execute(
                "INSERT INTO corrections VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    proposal_id,
                    self.owner,
                    memory_id,
                    expected,
                    body,
                    source,
                    "proposed",
                    self.db.clock(),
                    None,
                ),
            )
            return proposal_id

        return await self.db.write(insert)

    async def review(self, correction_id: str, accept: bool) -> Json:
        def commit(db: sqlite3.Connection) -> Json:
            row = db.execute(
                "SELECT * FROM corrections WHERE id=? AND owner_id=? AND status='proposed'",
                (correction_id, self.owner),
            ).fetchone()
            if row is None:
                raise Conflict("Correction unavailable or already decided")
            version = None
            if accept:
                version = self._edit(
                    db,
                    row["memory_id"],
                    row["expected_revision"],
                    row["body"],
                    f"review:{correction_id}:{row['source']}",
                    "owner",
                )
            db.execute(
                "UPDATE corrections SET status=?,decided_at=? WHERE id=?",
                ("accepted" if accept else "rejected", self.db.clock(), correction_id),
            )
            return {"status": "accepted" if accept else "rejected", "revision": version}

        return await self.db.write(commit)

    async def archive(self, memory_id: str) -> None:
        def archive(db: sqlite3.Connection) -> None:
            self._get(db, memory_id)
            db.execute(
                "UPDATE memory_records SET status='archived',updated_at=? WHERE id=?",
                (self.db.clock(), memory_id),
            )
            self.index_in(db, memory_id, 0, "", False)
            db.execute("DELETE FROM embeddings WHERE memory_id=?", (memory_id,))
            db.execute("DELETE FROM embedding_jobs WHERE memory_id=?", (memory_id,))
            self._invalidate(db)

        await self.db.write(archive)

    async def restore(self, memory_id: str, revision: int | None = None) -> int:
        def restore(db: sqlite3.Connection) -> int:
            row = self._get(db, memory_id)
            db.execute("UPDATE memory_records SET status='active' WHERE id=?", (memory_id,))
            if revision is not None:
                old = db.execute(
                    "SELECT body FROM memory_revisions WHERE memory_id=? AND version=?",
                    (memory_id, revision),
                ).fetchone()
                if old is None:
                    raise ValueError("Revision unavailable")
                return self._edit(
                    db, memory_id, row["revision"], old[0], f"restore:{revision}", "owner"
                )
            self.index_in(db, memory_id, row["revision"], row["body"])
            return int(row["revision"])

        return await self.db.write(restore)

    async def erase(self, memory_id: str) -> None:
        def erase(db: sqlite3.Connection) -> None:
            self._get(db, memory_id)
            db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
            # Erase all materialized context/checkpoints conservatively, never inspect only IDs.
            db.execute("DELETE FROM context_snapshots WHERE owner_id=?", (self.owner,))
            db.execute("DELETE FROM messages WHERE owner_id=? AND role='checkpoint'", (self.owner,))
            db.execute("UPDATE runs SET context_id=NULL WHERE owner_id=?", (self.owner,))
            self._invalidate(db)
            db.execute("DELETE FROM memory_records WHERE id=?", (memory_id,))

        await self.db.write(erase)

    async def connect(self, source_id: str, target_id: str, relation: str, source: str) -> str:
        edge_id = uid()

        def link(db: sqlite3.Connection) -> str:
            self._get(db, source_id)
            self._get(db, target_id)
            if relation == "supersedes":
                cycle = db.execute(
                    "WITH RECURSIVE descendants(id) AS (SELECT target_id FROM memory_edges WHERE source_id=? AND relation='supersedes' UNION SELECT e.target_id FROM memory_edges e JOIN descendants d ON e.source_id=d.id WHERE e.relation='supersedes') SELECT 1 FROM descendants WHERE id=?",
                    (target_id, source_id),
                ).fetchone()
                if cycle or source_id == target_id:
                    raise Conflict("Supersession cycle")
            db.execute(
                "INSERT INTO memory_edges VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_id,target_id,relation) DO NOTHING",
                (
                    edge_id,
                    self.owner,
                    source_id,
                    target_id,
                    relation,
                    source,
                    self.db.clock(),
                    None,
                ),
            )
            row = db.execute(
                "SELECT id FROM memory_edges WHERE source_id=? AND target_id=? AND relation=?",
                (source_id, target_id, relation),
            ).fetchone()
            assert row is not None
            return str(row[0])

        return await self.db.write(link)

    async def search(self, query: str, limit: int = 20) -> list[Json]:
        words = re.findall(r"\w+", query, re.UNICODE)[:40]
        if not words:
            return []
        fts = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
        return await self.db.read(
            "SELECT m.*,r.body,r.provenance,r.source,bm25(memory_fts) AS lexical_rank FROM memory_fts JOIN memory_records m ON m.id=memory_fts.memory_id JOIN memory_revisions r ON r.memory_id=m.id AND r.version=m.revision WHERE memory_fts MATCH ? AND m.owner_id=? AND m.status='active' ORDER BY lexical_rank LIMIT ?",
            (fts, self.owner, min(max(limit, 1), 50)),
        )

    async def neighbours(self, ids: list[str], depth: int = 2) -> list[Json]:
        seen = set(ids)
        found: list[Json] = []
        for _ in range(min(depth, 2)):
            next_ids: list[str] = []
            for memory_id in ids[:50]:
                rows = await self.db.read(
                    "SELECT m.*,r.body,r.provenance,r.source FROM memory_edges e JOIN memory_records m ON m.id=CASE WHEN e.source_id=? THEN e.target_id ELSE e.source_id END JOIN memory_revisions r ON r.memory_id=m.id AND r.version=m.revision WHERE e.owner_id=? AND (e.source_id=? OR e.target_id=?) AND m.status='active' AND e.valid_from<=? AND (e.valid_to IS NULL OR e.valid_to>?) LIMIT 3",
                    (memory_id, self.owner, memory_id, memory_id, self.db.clock(), self.db.clock()),
                )
                for row in rows:
                    if row["id"] not in seen:
                        seen.add(row["id"])
                        found.append(row)
                        next_ids.append(row["id"])
            ids = next_ids
        return found

    async def store_embedding(
        self, memory_id: str, revision: int, vector: list[float], model: str, preprocessing: str
    ) -> bool:
        if not vector or any(not math.isfinite(x) for x in vector):
            raise ValueError("Invalid embedding")
        packed = struct.pack(f"<{len(vector)}f", *vector)

        def store(db: sqlite3.Connection) -> bool:
            row = self._get(db, memory_id)
            if row["revision"] != revision or row["status"] != "active":
                return False
            db.execute(
                "INSERT INTO embeddings VALUES(?,?,?,?,?,?) ON CONFLICT(memory_id,model) DO UPDATE SET revision=excluded.revision,dimensions=excluded.dimensions,preprocessing=excluded.preprocessing,vector=excluded.vector",
                (memory_id, revision, model, len(vector), preprocessing, packed),
            )
            db.execute(
                "DELETE FROM embedding_jobs WHERE memory_id=? AND revision=?", (memory_id, revision)
            )
            return True

        return await self.db.write(store)

    async def set_fact(
        self,
        subject: str,
        predicate: str,
        value: str,
        source: str,
        *,
        expected: int = 0,
        valid_from: float | None = None,
        valid_to: float | None = None,
    ) -> str:
        def update(db: sqlite3.Connection) -> str:
            row = db.execute(
                "SELECT * FROM facts WHERE owner_id=? AND subject=? AND predicate=?",
                (self.owner, subject, predicate),
            ).fetchone()
            if (int(row["revision"]) if row else 0) != expected:
                raise Conflict("Fact revision changed")
            fact_id = str(row["id"]) if row else uid()
            version = expected + 1
            db.execute(
                "INSERT INTO facts VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id,subject,predicate) DO UPDATE SET revision=excluded.revision,status='active'",
                (fact_id, self.owner, subject, predicate, version, "active"),
            )
            db.execute(
                "INSERT INTO fact_revisions VALUES(?,?,?,?,?,?,?,?)",
                (
                    fact_id,
                    version,
                    value,
                    source,
                    "owner",
                    self.db.clock(),
                    valid_from if valid_from is not None else self.db.clock(),
                    valid_to,
                ),
            )
            self._invalidate(db)
            return fact_id

        return await self.db.write(update)

    async def current_facts(self) -> list[Json]:
        return await self.db.read(
            "SELECT f.*,r.value,r.source,r.provenance,r.valid_from,r.valid_to FROM facts f JOIN fact_revisions r ON r.fact_id=f.id AND r.version=f.revision WHERE f.owner_id=? AND f.status='active' AND r.valid_from<=? AND (r.valid_to IS NULL OR r.valid_to>?)",
            (self.owner, self.db.clock(), self.db.clock()),
        )
