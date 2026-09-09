"""Consistent SQLite snapshots and verified blob backups.

Creates and verifies manifests, restores into quarantine and applies retention.
These operator workflows never signal workers or promote application releases.
"""

import asyncio
import contextlib
import json
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from theo import __version__
from theo.config import Settings, save_settings
from theo.content.artifacts import scoped_path
from theo.domain import Conflict, Denied, Json, uid
from theo.execution.files import file_hash
from theo.storage import Database


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
            await restored.execute(
                "UPDATE outbox SET status='uncertain',error='restored_snapshot' WHERE status IN ('ready','executing') AND action_id IN (SELECT id FROM actions WHERE status='uncertain')"
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
