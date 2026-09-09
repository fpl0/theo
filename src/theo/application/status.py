"""Read the current daemon, work queue and unresolved delivery status.

Provides the shared status projection for operator and conversation interfaces
without importing daemon startup or native reasoning adapters.
"""

from theo.config import Settings
from theo.domain import (
    Json,
)
from theo.storage import Database


async def status(db: Database, settings: Settings) -> Json:
    owner = settings.owner_id
    controls = await db.read(
        "SELECT key,value FROM control WHERE owner_id=? AND key IN ('background_paused','models_paused','notifications_paused','quarantined')",
        (owner,),
    )
    jobs = await db.read(
        "SELECT status,count(*) count FROM jobs WHERE owner_id=? GROUP BY status", (owner,)
    )
    unresolved = await db.one(
        "SELECT count(*) count FROM actions WHERE owner_id=? AND status='uncertain'", (owner,)
    )
    heartbeat = await db.one(
        "SELECT max(heartbeat_at) heartbeat FROM lifecycle_intervals WHERE owner_id=?", (owner,)
    )
    return {
        "name": settings.name,
        "core_healthy": bool(
            heartbeat and heartbeat["heartbeat"] and db.clock() - heartbeat["heartbeat"] < 60
        ),
        "controls": controls,
        "jobs": jobs,
        "uncertain_actions": unresolved["count"] if unresolved else 0,
        "native_execution": "requires verified included account and OS isolation",
        "memory_retrieval": "semantic_assets_present"
        if (db.root / "models/embeddings/manifest.json").exists()
        else "fts_only",
    }
