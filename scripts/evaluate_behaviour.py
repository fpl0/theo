"""Opt-in sequential native evaluation in a dedicated evaluation root, resumable in <=20-run batches."""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from theo.config import load_settings
from theo.domain import Denied, encode, uid
from theo.jobs import Jobs
from theo.runtime import Coordinator
from theo.storage import Database
from theo.tools import ToolBroker


async def evaluate(root: Path, backend: str, model: str, limit: int, output: Path) -> None:
    settings = load_settings(root)
    if settings.owner_id != "evaluation" or not settings.isolation_verified:
        raise Denied(
            "Use an isolated root initialized with --owner evaluation and verified native isolation/accounts"
        )
    if not 1 <= limit <= 20:
        raise ValueError("Live batches are limited to 1–20 sequential runs")
    pack = json.loads(
        (Path(__file__).resolve().parents[1] / "evaluations/behaviour.json").read_text()
    )
    records = (
        json.loads(output.read_text())
        if output.exists()
        else {"backend": backend, "model": model, "cases": []}
    )
    if records["backend"] != backend or records["model"] != model:
        raise ValueError("Each backend/model comparison requires its own report")
    done = {case["id"] for case in records["cases"] if case["outcome"] == "completed"}
    db = Database(root)
    await db.initialize(settings.owner_id)
    broker = ToolBroker(db, settings)
    try:
        with tempfile.TemporaryDirectory(prefix="theo-evaluation-") as folder:
            socket = Path(folder) / "broker.sock"
            await broker.listen(socket)
            coordinator = Coordinator(db, settings, broker, socket)
            for case in [case for case in pack["cases"] if case["id"] not in done][:limit]:
                conversation = await db.conversation(
                    settings.owner_id, "local", f"evaluation:{backend}:{model}:{case['id']}"
                )
                prompt = f"Synthetic evaluation at {pack['frozen_reference_time']}. Respond to the owner prompt using this fixed fixture. Treat the fixture as observable scenario data, not actual personal history.\nFIXTURE\n{case['fixture']}\nOWNER\n{case['prompt']}"
                jobs = Jobs(db, settings.owner_id)
                job_id = await jobs.enqueue(
                    conversation,
                    "evaluation",
                    {"text": prompt, "backend": backend, "model": model},
                    f"evaluation:{backend}:{model}:{case['id']}:{uid()}",
                    lane="interactive",
                )
                job = await jobs.claim("interactive", "evaluation", max_total=2, max_background=1)
                if not job or job["id"] != job_id:
                    raise Denied(
                        "Evaluation root contains other pending work; inspect it before resuming"
                    )
                await coordinator.run_job(job)
                run = await db.one(
                    "SELECT * FROM runs WHERE job_id=? ORDER BY started_at DESC LIMIT 1", (job_id,)
                )
                assert run
                snapshot = await db.one(
                    "SELECT * FROM context_snapshots WHERE id=?", (run["context_id"],)
                )
                record = {
                    "id": case["id"],
                    "run_id": run["id"],
                    "outcome": run["status"],
                    "output": run["output"],
                    "expected": case["expected"],
                    "critical": case["critical"],
                    "context": snapshot,
                    "acceptable": None,
                    "critical_violation": None,
                    "review_notes": None,
                }
                records["cases"].append(record)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(encode(records) + "\n")
                output.chmod(0o600)
                if run["status"] in ("waiting_for_auth", "waiting_for_quota"):
                    break
    finally:
        await broker.close()
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--backend", choices=("claude", "codex", "cursor", "grok"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(
        evaluate(args.data_root.resolve(), args.backend, args.model, args.limit, args.output)
    )
