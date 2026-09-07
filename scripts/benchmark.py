"""Reproducible host-only capacity probe. Never opens a model account or a production root."""

import argparse
import asyncio
import json
import platform
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

from theo.config import Settings
from theo.context import ContextAssembler
from theo.memory import Memory
from theo.operations import backup_create, backup_verify, restore_backup
from theo.storage import Database


async def main(output: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="theo-capacity-") as folder:
        root = Path(folder)
        db = Database(root / "data")
        await db.initialize()
        conversation = await db.conversation("owner", "local", "capacity")
        memory = Memory(db, "owner")
        started = time.perf_counter()

        def populate(connection):
            now = time.time()
            for i in range(20_000):
                key = f"fixture-{i}"
                body = f"Project nebula topic{i % 200} evidence number {i}; reviewed synthetic source and next action."
                connection.execute(
                    "INSERT INTO memory_records VALUES(?,?,?,?,?,?,?,?,?)",
                    (key, "owner", "episode", 1, "active", 0.5, 0, now, now),
                )
                connection.execute(
                    "INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?)",
                    (key, 1, body, "owner", "synthetic:capacity", "{}", now),
                )
                memory.index_in(connection, key, 1, body)
            for i in range(250_000):
                Database.append_message(
                    connection,
                    "owner",
                    conversation,
                    "user" if i % 2 else "assistant",
                    f"Synthetic message {i}: topic{i % 200}",
                    now,
                )

        try:
            await db.write(populate)
            population = time.perf_counter() - started
            times = []
            assembler = ContextAssembler(db, "owner")
            await assembler.assemble(conversation, "topic7")
            for i in range(50):
                started = time.perf_counter()
                await assembler.assemble(conversation, f"topic{i % 200}")
                times.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            # Synthetic benchmark volume only; does not attest real storage encryption.
            settings = Settings(encrypted_storage_verified=True)
            snapshot = await backup_create(db, settings)
            await backup_verify(snapshot)
            backup_seconds = time.perf_counter() - started
            started = time.perf_counter()
            await restore_backup(snapshot, root / "restored", settings)
            restore_seconds = time.perf_counter() - started
            result = {
                "fixture": {"memories": 20_000, "messages": 250_000, "synthetic": True},
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "population_seconds": population,
                "retrieval": "FTS + graph; embeddings absent (not the full warm-vector acceptance gate)",
                "context_runs": 50,
                "context_p50_ms": statistics.median(times),
                "context_p95_ms": sorted(times)[47],
                "context_max_ms": max(times),
                "backup_verify_seconds": backup_seconds,
                "restore_quarantine_seconds": restore_seconds,
                "live_model_calls": 0,
                "target_mac_qualified": False,
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
        finally:
            await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args().output))
