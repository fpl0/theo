"""Persist process birth identity so recovery never signals a recycled PID."""

import asyncio

import psutil

from theo.domain import Json
from theo.storage import Database
from theo.supervisor import terminate_tree


async def register_worker(db: Database, owner: str, run_id: str, pid: int) -> None:
    birth = psutil.Process(pid).create_time()
    await db.execute(
        "INSERT OR REPLACE INTO worker_processes VALUES(?,?,?,?,?)",
        (run_id, owner, pid, birth, db.clock()),
    )


async def terminate_registered(db: Database, owner: str) -> Json:
    rows = await db.read("SELECT * FROM worker_processes WHERE owner_id=?", (owner,))
    stopped = 0
    for row in rows:
        try:
            process = psutil.Process(row["pid"])
            if abs(process.create_time() - row["birth_time"]) < 0.01:
                await asyncio.to_thread(terminate_tree, row["pid"])
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    raise RuntimeError(
                        "Old native worker could not be terminated; recovery is blocked"
                    )
                stopped += 1
        except psutil.NoSuchProcess:
            pass
        await db.execute("DELETE FROM worker_processes WHERE run_id=?", (row["run_id"],))
    return {"terminated": stopped}
