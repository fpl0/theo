"""Read operator diagnostics for storage, assets, accounts and qualification.

Combines service status with local installation checks without executing a model
or changing operational controls.
"""

import platform
import shutil
import sqlite3

from theo import __version__
from theo.application.status import status
from theo.backends.policy import BACKENDS, Accounts
from theo.config import Settings
from theo.domain import Json
from theo.storage import Database


async def doctor(db: Database, settings: Settings) -> Json:
    from theo.tools.registry import REGISTRY

    checks: Json = {
        "application": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "disk_free_bytes": shutil.disk_usage(db.root).free,
        "schema": await db.read("SELECT * FROM schema_migrations"),
        "tools": len(REGISTRY),
        "isolation_verified": settings.isolation_verified,
        "encrypted_storage_verified": settings.encrypted_storage_verified,
    }
    checks["database_integrity"] = await db.read("PRAGMA integrity_check")
    checks["foreign_key_violations"] = await db.read("PRAGMA foreign_key_check")
    checks["backends"] = {
        name: {"installed": shutil.which("agent" if name == "cursor" else name) is not None}
        for name in BACKENDS
    }
    checks["accounts"] = await Accounts(db, settings.owner_id).usage()
    checks["status"] = await status(db, settings)
    from theo.operations.qualification import qualification_status

    checks["qualification"] = await qualification_status(db, settings)
    checks["production_qualified"] = checks["qualification"]["production_qualified"]
    checks["assets"] = {
        "embeddings": (db.root / "models/embeddings/manifest.json").exists(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
    }
    return checks
