"""Read the current daemon, work queue and unresolved delivery status.

Provides the shared status projection for operator and conversation interfaces
without importing daemon startup or native reasoning adapters.
"""

from theo.config import Settings
from theo.domain import (
    Json,
)
from theo.storage import Database
from theo.work.jobs import Jobs


async def status(
    db: Database,
    settings: Settings,
    *,
    scope: str | None = None,
    exclude_job_id: str | None = None,
) -> Json:
    owner = settings.owner_id
    work = await Jobs(db, owner).inspect(scope=scope, exclude_job_id=exclude_job_id)
    unresolved = await db.one(
        "SELECT count(*) count FROM actions WHERE owner_id=? AND status='uncertain' AND (? IS NULL OR conversation_id=?)",
        (owner, scope, scope),
    )
    heartbeat = await db.one(
        "SELECT max(heartbeat_at) heartbeat FROM lifecycle_intervals WHERE owner_id=?", (owner,)
    )
    return {
        "name": settings.name,
        "core_healthy": bool(
            heartbeat and heartbeat["heartbeat"] and db.clock() - heartbeat["heartbeat"] < 60
        ),
        **work,
        "uncertain_actions": unresolved["count"] if unresolved else 0,
        "native_execution": "requires verified included account and OS isolation",
        "memory_retrieval": "semantic_assets_present"
        if (db.root / "models/embeddings/manifest.json").exists()
        else "fts_only",
    }
