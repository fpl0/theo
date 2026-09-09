import asyncio
import sys

from theo.work.jobs import Jobs


async def test_a16_committed_inbox_survives_real_process_kill(db, conversation):
    code = """
import asyncio,sys
from pathlib import Path
from theo.storage import Database
from theo.work.jobs import Jobs
async def main():
 db=Database(Path(sys.argv[1]))
 await db.initialize()
 await Jobs(db,"owner").ingest(sys.argv[2],"telegram","77",{"fixture":True},"durable crash input")
 print("COMMITTED",flush=True)
 await asyncio.Event().wait()
asyncio.run(main())
"""
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(db.root),
        conversation,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(worker.stdout.readline(), 10) == b"COMMITTED\n"
        worker.kill()
        await worker.wait()
        await Jobs(db, "owner").ingest(
            conversation, "telegram", "77", {"fixture": True}, "durable crash input"
        )
        assert (await db.one("SELECT count(*) n FROM jobs"))["n"] == 1
        assert (
            await db.one("SELECT count(*) n FROM messages WHERE content='durable crash input'")
        )["n"] == 1
    finally:
        if worker.returncode is None:
            worker.kill()
            await worker.wait()
