"""Read-only Luke snapshot import with restartable mappings and conflict quarantine."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from theo.domain import Denied, Json, digest, encode, uid
from theo.memory import Memory
from theo.operations import file_hash
from theo.storage import Database


def strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            return text[end + 4 :].lstrip("\r\n")
    return text


def inspect_snapshot(source: Path) -> Json:
    source = source.resolve()
    if not source.is_dir():
        raise ValueError("Luke source must be a read-only snapshot directory")
    if list(source.glob("**/*-wal")) or list(source.glob("**/*-shm")):
        raise Denied("Use a consistent offline snapshot, not an open Luke database")
    excluded: list[str] = []
    files: dict[str, str] = {}
    for path in source.rglob("*"):
        if path.is_symlink():
            excluded.append(str(path.relative_to(source)))
            continue
        if not path.is_file():
            continue
        relative = str(path.relative_to(source))
        if any(part.startswith(".") for part in path.relative_to(source).parts) or any(
            x in path.name.lower() for x in ("credential", "token", "auth", "session")
        ):
            excluded.append(relative)
            continue
        files[relative] = file_hash(path)
    memory_files = {
        Path(name).stem: source / name
        for name in files
        if name.endswith(".md") and "memory" in name.split("/")
    }
    records: dict[str, Json] = {}
    links: list[Json] = []
    schedules: list[Json] = []
    histories: dict[str, list[Json]] = {}
    for name in files:
        if not name.endswith((".db", ".sqlite", ".sqlite3")):
            continue
        db = sqlite3.connect(f"file:{source / name}?mode=ro&immutable=1", uri=True)
        db.row_factory = sqlite3.Row
        try:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
            if "memory_meta" in tables:
                for row in db.execute("SELECT * FROM memory_meta"):
                    records[str(row["id"])] = dict(row)
            if "memory_fts" in tables:
                for row in db.execute("SELECT id,type,content FROM memory_fts"):
                    record = records.setdefault(
                        str(row["id"]),
                        {"id": str(row["id"]), "type": row["type"], "status": "active"},
                    )
                    record["database_body"] = row["content"]
            if "memory_history" in tables:
                for row in db.execute("SELECT * FROM memory_history ORDER BY id"):
                    histories.setdefault(str(row["mem_id"]), []).append(dict(row))
            if "memory_links" in tables:
                links.extend(dict(row) for row in db.execute("SELECT * FROM memory_links"))
            if "tasks" in tables:
                schedules.extend(dict(row) for row in db.execute("SELECT * FROM tasks"))
        finally:
            db.close()
    for external_id, path in memory_files.items():
        records.setdefault(
            external_id, {"id": external_id, "type": path.parent.name, "status": "active"}
        )["file_body"] = strip_frontmatter(path.read_text())
    accepted: list[Json] = []
    quarantined: list[Json] = []
    for external_id, record in records.items():
        database_body, file_body = record.get("database_body"), record.get("file_body")
        reason = None
        if database_body and file_body and database_body.strip() != file_body.strip():
            reason = "database_file_mismatch"
        body = file_body or database_body
        if not body:
            reason = "missing_body"
        for key in ("created", "updated"):
            if record.get(key):
                try:
                    datetime.fromisoformat(record[key].replace("Z", "+00:00"))
                except ValueError:
                    reason = "invalid_timestamp"
        item = {
            "external_id": external_id,
            "record": record,
            "body": body,
            "history": histories.get(external_id, []),
            "hash": digest({"record": record, "history": histories.get(external_id, [])}),
        }
        if reason:
            quarantined.append({**item, "reason": reason})
        else:
            accepted.append(item)
    scripts = [name for name in files if name.endswith((".sh", ".py"))]
    return {
        "source_hash": digest(files),
        "files": files,
        "accepted": accepted,
        "quarantined": quarantined,
        "links": links,
        "schedules": schedules,
        "excluded": excluded,
        "runtime_scripts_not_executed": scripts,
        "counts": {
            "accepted": len(accepted),
            "quarantined": len(quarantined),
            "tombstones": sum(x["record"].get("status") != "active" for x in accepted),
            "links": len(links),
            "schedules": len(schedules),
            "historical_revisions": sum(len(x["history"]) for x in accepted),
        },
    }


async def import_luke(db: Database, owner: str, source: Path, apply: bool = False) -> Json:
    if source.resolve() == db.root or source.resolve().is_relative_to(db.root):
        raise Denied("Import source cannot be the active Theo data root")
    report = inspect_snapshot(source)
    if not apply:
        return report
    import_id = uid()
    await db.execute(
        "INSERT OR IGNORE INTO imports VALUES(?,?,?,?,?)",
        (
            import_id,
            owner,
            report["source_hash"],
            encode({"files": report["files"], "counts": report["counts"]}),
            db.clock(),
        ),
    )
    row = await db.one(
        "SELECT id FROM imports WHERE owner_id=? AND source_hash=?", (owner, report["source_hash"])
    )
    assert row
    import_id = row["id"]
    imported, skipped = 0, 0
    for item in report["accepted"]:

        def commit(connection: sqlite3.Connection, item: Json = item) -> bool:
            existing = connection.execute(
                "SELECT 1 FROM import_items WHERE owner_id=? AND external_id=? AND source_hash=?",
                (owner, item["external_id"], item["hash"]),
            ).fetchone()
            if existing:
                return False
            older = connection.execute(
                "SELECT 1 FROM import_items WHERE owner_id=? AND external_id=? AND status='imported'",
                (owner, item["external_id"]),
            ).fetchone()
            if older:
                connection.execute(
                    "INSERT INTO import_items VALUES(?,?,?,?,?,?,?,?)",
                    (
                        uid(),
                        owner,
                        import_id,
                        item["external_id"],
                        item["hash"],
                        None,
                        "quarantined",
                        "changed_external_item",
                    ),
                )
                return False
            memory_id = uid()
            record = item["record"]
            bodies: list[str] = []
            for revision in item["history"]:
                for key in ("old_content", "new_content"):
                    if revision.get(key) and (not bodies or bodies[-1] != revision[key]):
                        bodies.append(revision[key])
            if not bodies or bodies[-1] != item["body"]:
                bodies.append(item["body"])
            active = record.get("status", "active") == "active"
            connection.execute(
                "INSERT INTO memory_records VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    memory_id,
                    owner,
                    record.get("type", "episode"),
                    len(bodies),
                    "active" if active else "archived",
                    min(1.0, max(0.0, float(record.get("importance", 0.5)))),
                    0,
                    db.clock(),
                    db.clock(),
                ),
            )
            for version, body in enumerate(bodies, 1):
                connection.execute(
                    "INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?)",
                    (
                        memory_id,
                        version,
                        body,
                        "import",
                        f"luke:{item['external_id']}",
                        encode({"source_record": record, "snapshot_hash": report["source_hash"]}),
                        db.clock(),
                    ),
                )
            Memory(db, owner).index_in(connection, memory_id, len(bodies), bodies[-1], active)
            connection.execute(
                "INSERT INTO import_items VALUES(?,?,?,?,?,?,?,?)",
                (
                    uid(),
                    owner,
                    import_id,
                    item["external_id"],
                    item["hash"],
                    memory_id,
                    "imported",
                    "full_bodies_in_sqlite",
                ),
            )
            return True

        if await db.write(commit):
            imported += 1
        else:
            skipped += 1
    for item in report["quarantined"]:
        await db.execute(
            "INSERT OR IGNORE INTO import_items VALUES(?,?,?,?,?,?,?,?)",
            (
                uid(),
                owner,
                import_id,
                item["external_id"],
                item["hash"],
                None,
                "quarantined",
                encode(item),
            ),
        )
    for link in report["links"]:
        source_map = await db.one(
            "SELECT target_id FROM import_items WHERE owner_id=? AND external_id=? AND status='imported' ORDER BY rowid DESC LIMIT 1",
            (owner, link["from_id"]),
        )
        target_map = await db.one(
            "SELECT target_id FROM import_items WHERE owner_id=? AND external_id=? AND status='imported' ORDER BY rowid DESC LIMIT 1",
            (owner, link["to_id"]),
        )
        if source_map and target_map:
            await Memory(db, owner).connect(
                source_map["target_id"],
                target_map["target_id"],
                link["relationship"],
                f"luke-import:{import_id}",
            )
            if link.get("valid_until"):
                try:
                    valid_to = (
                        datetime.fromisoformat(link["valid_until"].replace("Z", "+00:00"))
                        .replace(tzinfo=UTC)
                        .timestamp()
                    )
                    await db.execute(
                        "UPDATE memory_edges SET valid_to=? WHERE source_id=? AND target_id=? AND relation=?",
                        (
                            valid_to,
                            source_map["target_id"],
                            target_map["target_id"],
                            link["relationship"],
                        ),
                    )
                except ValueError:
                    await db.health(owner, "import_link_quarantined", {"import_id": import_id})
    # Ambiguous legacy schedule semantics are retained for review, never activated by guesswork.
    for schedule in report["schedules"]:
        await db.execute(
            "INSERT OR IGNORE INTO import_items VALUES(?,?,?,?,?,?,?,?)",
            (
                uid(),
                owner,
                import_id,
                "schedule:" + str(schedule["id"]),
                digest(schedule),
                None,
                "quarantined",
                encode(
                    {
                        "reason": "review_timezone_catchup_and_delivery_before_activation",
                        "schedule": schedule,
                    }
                ),
            ),
        )
    return {
        "import_id": import_id,
        "imported": imported,
        "skipped": skipped,
        "quarantined": len(report["quarantined"]) + len(report["schedules"]),
        "source_unchanged": inspect_snapshot(source)["source_hash"] == report["source_hash"],
    }
