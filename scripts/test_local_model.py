"""Opt-in real GGUF inference through Theo's core; no Telegram/native qualification."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import platform
import tempfile
import time
from pathlib import Path

from theo.backends.native import NativeBackend
from theo.config import Settings
from theo.delivery import Delivery
from theo.domain import ExecutionOutcome, Outcome
from theo.jobs import Jobs
from theo.memory import Memory
from theo.runtime import Coordinator
from theo.storage import Database
from theo.tools import ToolBroker


class LocalBroker(ToolBroker):
    def grant(self, context):
        return super().grant(
            context.model_copy(update={"tools": frozenset({"remember", "recall"})})
        )


class LocalModel(NativeBackend):
    name = "local-gguf-test"

    def __init__(self, db, settings, model, broker, traces):
        super().__init__(db, settings)
        self.model, self.broker, self.traces = model, broker, traces

    async def execute(self, request, emit):
        # Actual model-generated JSON, constrained only for syntax. Never supply answers.
        messages = [
            {
                "role": "system",
                "content": (
                    'You are Theo. Return JSON only: {"answer":"your reply"} or '
                    '{"tool":"remember","arguments":{"body":"memory text","kind":"episode"}} '
                    'or {"tool":"recall","arguments":{"query":"search words"}}. '
                    "After receiving a tool result, answer the user. Use only the two listed tools."
                ),
            },
            {"role": "user", "content": request.context},
        ]
        for _ in range(4):
            started = time.monotonic()
            result = await asyncio.to_thread(
                self.model.create_chat_completion,
                messages=messages,
                temperature=0,
                max_tokens=192,
                response_format={"type": "json_object"},
            )
            choice = result["choices"][0]
            raw = choice["message"]["content"]
            trace = {
                "run_id": request.run_id,
                "raw": raw,
                "finish_reason": choice["finish_reason"],
                "usage": result["usage"],
                "seconds": round(time.monotonic() - started, 3),
            }
            self.traces.append(trace)
            if choice["finish_reason"] != "stop":
                return ExecutionOutcome(status=Outcome.FAILED, error="Generation truncated")
            value = json.loads(raw)
            if not isinstance(value, dict) or not (
                isinstance(value.get("answer"), str)
                or (isinstance(value.get("tool"), str) and isinstance(value.get("arguments"), dict))
            ):
                return ExecutionOutcome(
                    status=Outcome.FAILED, error="Model returned an invalid tool-protocol object"
                )
            if "answer" in value:
                return ExecutionOutcome(status=Outcome.COMPLETED, text=str(value["answer"]))
            receipt = await self.broker.call(request.tool_token, value["tool"], value["arguments"])
            trace["tool"] = value["tool"]
            trace["receipt"] = receipt.model_dump(mode="json")
            messages.extend(
                [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "Tool result: " + receipt.model_dump_json()},
                ]
            )
        return ExecutionOutcome(status=Outcome.FAILED, error="Local test tool-turn limit")


async def run(args):
    from llama_cpp import Llama

    model = Llama(
        model_path=str(args.model),
        n_ctx=8192,
        n_threads=4,
        seed=42,
        chat_format="chatml",
        verbose=False,
    )
    report = {
        "scope": "real local inference; in-process core and broker; local delivery sink",
        "telegram_tested": False,
        "native_codex_tested": False,
        "model_file": args.model.name,
        "model_sha256": hashlib.file_digest(args.model.open("rb"), "sha256").hexdigest(),
        "python": platform.python_version(),
        "llama_cpp_python": importlib.metadata.version("llama-cpp-python"),
        "settings": {"n_ctx": 8192, "n_threads": 4, "temperature": 0, "seed": 42},
        "cases": [],
        "generations": [],
    }
    with tempfile.TemporaryDirectory(prefix="theo-live-local-") as temporary:
        db = Database(Path(temporary) / "data")
        await db.initialize()
        settings = Settings()
        broker = LocalBroker(db, settings)
        coordinator = Coordinator(
            db,
            settings,
            broker,
            Path(temporary) / "unused.sock",
            factory=lambda _: LocalModel(db, settings, model, broker, report["generations"]),
        )
        memory = Memory(db, "owner")
        await memory.remember(
            "Synthetic project Juniper has access phrase amber-otter-731.",
            source="owner:synthetic",
            provenance="owner",
            pinned=True,
        )
        conversation = await db.conversation("owner", "local", "synthetic")
        await db.execute(
            "UPDATE conversations SET backend=?,model=? WHERE id=?",
            ("local-gguf-test", args.model.name, conversation),
        )
        cases = [
            ("identity", "What is your name? Answer with your name only.", "Theo", None),
            (
                "canonical_memory",
                "What is the access phrase for project Juniper?",
                "amber-otter-731",
                None,
            ),
            (
                "remember_tool",
                "Use the remember tool to save this synthetic fact: Project Birch has mascot violet-lynx-284. Then acknowledge it.",
                None,
                "remember",
            ),
            (
                "recall_tool",
                "Use the recall tool to search Birch, then tell me its mascot.",
                "violet-lynx-284",
                "recall",
            ),
        ]
        try:
            for name, prompt, expected, tool in cases:
                job_id = await Jobs(db, "owner").ingest(conversation, "local", name, {}, prompt)
                job = await Jobs(db, "owner").claim("interactive", "live-local-test")
                assert job and job["id"] == job_id
                await coordinator.run_job(job)
                deliveries = []

                async def sink(operation, payload, deliveries=deliveries):
                    deliveries.append({"operation": operation, "payload": payload})
                    return {"message_id": len(deliveries), "channel": "local_test_sink"}

                while await Delivery(db, settings).dispatch_one(sink):
                    pass
                result = await db.one("SELECT * FROM runs WHERE job_id=?", (job_id,))
                traces = [x for x in report["generations"] if x["run_id"] == result["id"]]
                terminals = await db.one(
                    "SELECT count(*) n FROM run_events WHERE run_id=? AND kind='terminal'",
                    (result["id"],),
                )
                checks = {
                    "completed": result["status"] == "completed",
                    "single_terminal": terminals["n"] == 1,
                    "single_delivery": len(deliveries) == 1,
                    "answer": expected is None or expected.lower() in result["output"].lower(),
                    "tool_committed": tool is None
                    or any(
                        t.get("tool") == tool and t["receipt"]["status"] in {"committed", "ok"}
                        for t in traces
                    ),
                }
                report["cases"].append(
                    {
                        "name": name,
                        "prompt": prompt,
                        "output": result["output"],
                        "error": result["error"],
                        "checks": checks,
                        "passed": all(checks.values()),
                    }
                )
                print(json.dumps(report["cases"][-1]), flush=True)
        finally:
            await broker.close()
            await db.close()
            model.close()
    report["passed"] = all(c["passed"] for c in report["cases"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
