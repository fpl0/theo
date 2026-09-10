"""Perform bounded supervisor database operations under the core service identity.

The privileged supervisor never opens SQLite or follows mutable core paths with
root authority. This pinned helper drops through subprocess identity options
before startup and has only the core account's filesystem access.
"""

import argparse
import asyncio
import fcntl
import json
import os
import sys
from pathlib import Path
from typing import cast

from theo.config import Settings, load_settings
from theo.domain import Denied, Json, encode
from theo.operations.backups import backup_create
from theo.storage import Database
from theo.work.jobs import Jobs


async def operation(db: Database, settings: Settings, name: str, body: Json) -> Json:
    owner = settings.owner_id
    if name == "control" and set(body) == {"key"}:
        if body["key"] not in ("deployments_paused", "quarantined", "models_paused"):
            raise Denied("Unsupported supervisor control read")
        return {"value": await db.control(owner, body["key"])}
    if name == "draining" and set(body) == {"paused"} and type(body["paused"]) is bool:
        await db.set_control(owner, "maintenance_draining", str(body["paused"]).lower())
        return {"saved": True}
    if name == "schema" and not body:
        row = await db.one("SELECT max(version) n FROM schema_migrations")
        return {"version": row["n"] if row else None}
    if name == "integrity" and not body:
        row = await db.one("PRAGMA integrity_check")
        return {"ok": bool(row and list(row.values()) == ["ok"])}
    if name == "heartbeat" and not body:
        try:
            with (db.root / "heartbeat.json").open() as stream:
                value = json.loads(stream.read(65537))
            if not isinstance(value, dict):
                raise ValueError("Invalid heartbeat")
            return {"pid": value["pid"], "timestamp": value["timestamp"]}
        except OSError, ValueError, TypeError, KeyError:
            return {"pid": None, "timestamp": None}
    if name == "paused" and not body:
        return {"paused": (db.root / "maintenance.pause").exists()}
    if name == "daemon_stopped" and not body:
        with (db.root / "daemon.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"stopped": False}
        return {"stopped": True}
    if name == "quiescent" and not body:
        row = await db.one(
            "SELECT (SELECT count(*) FROM jobs WHERE status='running')+"
            "(SELECT count(*) FROM outbox WHERE status='executing')+"
            "(SELECT count(*) FROM actions WHERE status='executing') n"
        )
        return {"ready": bool(row and row["n"] == 0)}
    if name == "backup" and not body:
        path = await backup_create(db, settings, release_snapshot=True)
        return {"path": str(path)}
    if name == "canary_create" and set(body) == {"change_id", "bundle_id", "deadline"}:
        conversation = await db.conversation(
            owner, "local", "maintenance-canary:" + body["change_id"]
        )
        job = await Jobs(db, owner).enqueue(
            conversation,
            "maintenance_canary",
            {
                "text": "Synthetic deployment check. Call get_status exactly once and then say checked. Do not save memories or send messages."
            },
            "activation-canary:" + body["change_id"] + ":" + body["bundle_id"],
            lane="interactive",
            deadline=body["deadline"],
            origin="system",
        )
        return {"job_id": job}
    if name == "canary_status" and set(body) == {"job_id"}:
        job = await db.one("SELECT status FROM jobs WHERE id=?", (body["job_id"],))
        receipt = await db.one(
            "SELECT m.content FROM messages m JOIN runs r ON r.id=m.run_id JOIN jobs j ON j.id=r.job_id AND j.generation=r.generation WHERE r.job_id=? AND m.source='tool:get_status' ORDER BY m.created_at DESC LIMIT 1",
            (body["job_id"],),
        )
        succeeded = False
        if job and job["status"] == "completed" and receipt:
            result = json.loads(receipt["content"])["result"]
            succeeded = (
                result["status"] not in {"failed", "denied", "invalid", "uncertain"}
                and result.get("data") is not None
            )
        return {"status": job["status"] if job else None, "tool_succeeded": succeeded}
    if name == "retry_canary" and set(body) == {"job_id", "available_at", "deadline"}:
        await db.execute(
            "UPDATE jobs SET status='queued',generation=generation+1,available_at=?,deadline=? WHERE id=? AND status IN ('waiting_for_auth','waiting_for_quota')",
            (body["available_at"], body["deadline"], body["job_id"]),
        )
        return {"saved": True}
    raise Denied("Unsupported supervisor database operation")


async def run(root: Path, owner: str, name: str, body: Json) -> Json:
    settings = load_settings(root)
    if settings.owner_id != owner:
        raise Denied("Core settings no longer match the protected installation owner")
    db = Database(root)
    try:
        return await operation(db, settings, name, body)
    finally:
        await db.close()


def main() -> None:
    if os.geteuid() == 0:
        raise Denied("Core database operations must not run as root")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("operation")
    args = parser.parse_args()
    request = sys.stdin.buffer.read(65537)
    if len(request) > 65536:
        raise Denied("Supervisor database request exceeded its bound")
    body = json.loads(request)
    if not isinstance(body, dict):
        raise Denied("Supervisor database request must be an object")
    print(
        encode(asyncio.run(run(args.root, args.owner, args.operation, cast(Json, body)))),
        flush=True,
    )


if __name__ == "__main__":
    main()
