"""Stage and switch schema-compatible application releases.

Validates release manifests and records recovery state; backup creation and data
export are separate operator services.
"""

import json
import os
import shutil
import sqlite3
from pathlib import Path

from theo.config import Settings
from theo.content.artifacts import scoped_path
from theo.domain import Conflict, Denied, Json, uid
from theo.execution.files import file_hash
from theo.operations.backups import backup_create
from theo.storage import Database


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
