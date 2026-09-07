"""Consistent backups, quarantine restore, exports and schema-compatible releases."""

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from theo import __version__
from theo.artifacts import scoped_path
from theo.config import Settings, save_settings
from theo.domain import Conflict, Denied, Json, encode, uid
from theo.storage import Database


def file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


async def snapshot_database(db: Database, destination: Path) -> None:
    def backup() -> None:
        with (
            contextlib.closing(sqlite3.connect(db.path.as_uri() + "?mode=ro", uri=True)) as source,
            contextlib.closing(sqlite3.connect(destination)) as target,
        ):
            source.backup(target, pages=256, sleep=0.01)
            result = target.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise ValueError("Snapshot integrity check failed")
            if target.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Snapshot foreign key check failed")
        destination.chmod(0o600)

    await asyncio.to_thread(backup)


async def backup_create(db: Database, settings: Settings, destination: Path | None = None) -> Path:
    if not settings.encrypted_storage_verified:
        raise Denied("Verify encrypted owner storage before persisting personal-data backups")
    base = destination or db.root / "backups"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = base / (
        datetime.fromtimestamp(db.clock(), UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uid()[:8]
    )
    temporary = Path(tempfile.mkdtemp(prefix=".preparing-", dir=base))
    try:
        database = temporary / "theo.sqlite3"
        await snapshot_database(db, database)
        connection = sqlite3.connect(database)
        try:
            rows = connection.execute(
                "SELECT hash,location,size FROM blobs WHERE location IS NOT NULL AND status='available'"
            ).fetchall()
            versions = connection.execute(
                "SELECT version,checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            connection.close()
        blobs: list[Json] = []
        for content_hash, location, size in rows:
            source = scoped_path(db.root, location)
            target = scoped_path(temporary, location)
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copyfile, source, target)
            if await asyncio.to_thread(file_hash, target) != content_hash:
                raise ValueError("External blob checksum mismatch")
            blobs.append({"hash": content_hash, "location": location, "size": size})
        manifest = {
            "format": 1,
            "application_version": __version__,
            "created_at": db.clock(),
            "database_sha256": await asyncio.to_thread(file_hash, database),
            "schema": versions,
            "blobs": blobs,
            "encrypted_storage": "operator_verified",
        }
        (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2))
        temporary.rename(path)
        await db.health(
            settings.owner_id, "backup_verified", {"path": str(path), "blob_count": len(blobs)}
        )
        return path
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


async def backup_verify(path: Path) -> Json:
    def verify() -> Json:
        manifest = json.loads((path / "manifest.json").read_text())
        if (
            manifest.get("format") != 1
            or file_hash(path / "theo.sqlite3") != manifest["database_sha256"]
        ):
            raise ValueError("Backup manifest/database checksum mismatch")
        connection = sqlite3.connect(
            (path / "theo.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
        )
        try:
            if (
                connection.execute("PRAGMA integrity_check").fetchone() != ("ok",)
                or connection.execute("PRAGMA foreign_key_check").fetchone()
            ):
                raise ValueError("Backup database integrity failure")
            expected = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    "SELECT hash,location,size FROM blobs WHERE location IS NOT NULL AND status='available'"
                )
            }
        finally:
            connection.close()
        actual = {blob["hash"]: (blob["location"], blob["size"]) for blob in manifest["blobs"]}
        if expected != actual:
            raise ValueError("Backup blob manifest is incomplete")
        for content_hash, (location, size) in expected.items():
            blob_path = scoped_path(path, location)
            if blob_path.stat().st_size != size or file_hash(blob_path) != content_hash:
                raise ValueError("Backup blob checksum failure")
        return {
            "verified": True,
            "database_integrity": "ok",
            "external_blobs": len(expected),
            "created_at": manifest["created_at"],
        }

    return await asyncio.to_thread(verify)


async def restore_backup(source: Path, target: Path, settings: Settings) -> Json:
    report = await backup_verify(source)
    if target.exists():
        raise Conflict("Restore target must be a new directory")
    target.mkdir(parents=True, mode=0o700)
    try:
        shutil.copyfile(source / "theo.sqlite3", target / "theo.sqlite3")
        if (source / "blobs").exists():
            shutil.copytree(source / "blobs", target / "blobs")
        restored = Database(target)
        try:
            await restored.initialize(settings.owner_id, settings.timezone)
            await restored.set_control(settings.owner_id, "quarantined", "true")
            await restored.set_control(settings.owner_id, "background_paused", "true")
            await restored.set_control(settings.owner_id, "notifications_paused", "true")
            await restored.set_control(
                settings.owner_id, "recovery_since", str(report["created_at"])
            )
            await restored.execute("UPDATE backend_accounts SET status='requires_reverification'")
            await restored.execute(
                "UPDATE jobs SET status='uncertain',generation=generation+1 WHERE status IN ('running','queued','interrupted')"
            )
            await restored.execute(
                "UPDATE actions SET status='uncertain' WHERE status IN ('ready','executing')"
            )
            await restored.health(
                settings.owner_id, "restore_quarantine", {"snapshot_time": report["created_at"]}
            )
        finally:
            await restored.close()
        save_settings(
            target, settings.model_copy(update={"qualified_backends": (), "soak_completed": False})
        )
        return {
            **report,
            "target": str(target),
            "outbound": "quarantined",
            "accounts": "reverification_required",
        }
    except BaseException:
        shutil.rmtree(target)
        raise


async def export_data(db: Database, target: Path, format: str = "jsonl") -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="theo-export-") as directory:
        snapshot = Path(directory) / "snapshot.sqlite3"
        await snapshot_database(db, snapshot)
        connection = sqlite3.connect(snapshot)
        connection.row_factory = sqlite3.Row
        try:
            with target.open("w") as stream:
                if format == "markdown":
                    stream.write(
                        "# Theo memory export\n\nRead-only projection; JSONL or a backup preserves all structured history.\n\n"
                    )
                    for row in connection.execute(
                        "SELECT m.id,m.kind,m.status,r.version,r.body FROM memory_records m JOIN memory_revisions r ON r.memory_id=m.id ORDER BY m.id,r.version"
                    ):
                        stream.write(
                            f"## {row['id']} / revision {row['version']} / {row['status']}\n\n{row['body']}\n\n"
                        )
                else:
                    stream.write(
                        encode(
                            {
                                "type": "manifest",
                                "format": 1,
                                "application": __version__,
                                "external_blobs": "Use a full backup for original external media",
                            }
                        )
                        + "\n"
                    )
                    tables = [
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'memory_fts%' ORDER BY name"
                        )
                    ]
                    for table in tables:
                        for row in connection.execute(
                            'SELECT * FROM "' + table.replace('"', '""') + '"'
                        ):
                            data = {
                                key: {"base64": base64.b64encode(value).decode()}
                                if isinstance(value, bytes)
                                else value
                                for key, value in dict(row).items()
                            }
                            stream.write(encode({"table": table, "record": data}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(0o600)
        finally:
            connection.close()
    return target


async def release_recovery(db: Database, settings: Settings, snapshot_time: float) -> Json:
    recorded = await db.control(settings.owner_id, "recovery_since")
    if recorded is None or float(recorded) != snapshot_time:
        raise Denied("Inspect and acknowledge the exact restored snapshot time")

    def release(connection: sqlite3.Connection) -> None:
        for table, states in (
            ("jobs", "'uncertain','running'"),
            ("actions", "'uncertain','executing'"),
            ("outbox", "'uncertain','executing'"),
        ):
            unresolved = connection.execute(
                f"SELECT count(*) FROM {table} WHERE owner_id=? AND status IN ({states})",
                (settings.owner_id,),
            ).fetchone()
            if unresolved and unresolved[0]:
                raise Denied(
                    "Reconcile remote effects and cancel unresolved restored jobs before releasing quarantine"
                )
        connection.execute(
            "UPDATE control SET value='false' WHERE owner_id=? AND key IN ('quarantined','notifications_paused')",
            (settings.owner_id,),
        )

    await db.write(release)
    return {"quarantined": False, "background": "paused", "reviewed_snapshot_time": snapshot_time}


def retain_backups(base: Path) -> int:
    snapshots: list[tuple[float, Path]] = []
    for manifest in base.glob("*/manifest.json"):
        try:
            snapshots.append(
                (float(json.loads(manifest.read_text())["created_at"]), manifest.parent)
            )
        except ValueError, KeyError:
            continue
    snapshots.sort(reverse=True)
    keep = {path for _, path in snapshots[:24]}
    days: set[str] = set()
    for timestamp, path in snapshots:
        day = datetime.fromtimestamp(timestamp, UTC).date().isoformat()
        if day not in days and len(days) < 14:
            keep.add(path)
            days.add(day)
    removed = 0
    for _, path in snapshots:
        if path not in keep:
            shutil.rmtree(path)
            removed += 1
    return removed


class Releases:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self.root = db.root / "releases"

    async def stage(self, release: Path) -> Json:
        manifest = json.loads((release / "release.json").read_text())
        required = {
            "id",
            "version",
            "source_commit",
            "lock_sha256",
            "schema_min",
            "schema_max",
            "files",
            "canary_passed",
        }
        if set(manifest) != required or not manifest["canary_passed"]:
            raise Denied("Release must contain a validated manifest and passed canary")
        if not all(x.isalnum() or x in "-_." for x in manifest["id"]):
            raise ValueError("Unsafe release ID")
        for name, checksum in manifest["files"].items():
            if file_hash(scoped_path(release, name)) != checksum:
                raise ValueError("Release content hash mismatch")
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / manifest["id"]
        if not target.exists():
            shutil.copytree(release, target)
        return manifest

    async def switch(self, release_id: str) -> Json:
        target = scoped_path(self.root, release_id)
        manifest = await self.stage(target)
        row = await self.db.one("SELECT max(version) version FROM schema_migrations")
        schema = row["version"] if row else 0
        if not manifest["schema_min"] <= schema <= manifest["schema_max"]:
            raise Denied(
                "Release is incompatible with the current schema; automatic database rollback is prohibited"
            )
        running = await self.db.one("SELECT count(*) n FROM jobs WHERE status='running'")
        if running and running["n"]:
            raise Conflict("Drain all workers before switching the release")
        await backup_create(self.db, self.settings)
        pointer = self.root / "current"
        old = os.readlink(pointer) if pointer.is_symlink() else None
        temporary = self.root / (".current-" + uid())
        temporary.symlink_to(target.name, target_is_directory=True)
        temporary.replace(pointer)
        await self.db.set_control(self.settings.owner_id, "background_paused", "true")
        await self.db.health(
            self.settings.owner_id, "release_switched", {"from": old, "to": release_id}
        )
        return {"release": release_id, "previous": old, "schema": schema, "autonomy": "paused"}
