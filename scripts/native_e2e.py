"""Opt-in LOCAL subscription tests of the real native adapters and core.

Runs synthetic inputs through Jobs -> Coordinator -> native protocol -> MCP ->
SQLite -> Delivery. Uses the operator's existing native subscription login and
native runtime permissions, with only remember/recall granted by the broker.
The deployment account-attestation and external OS-isolation gates are replaced
only inside this throwaway test harness; no production settings are changed.
This is adapter integration evidence, not deployment or Telegram qualification.
"""

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from theo.application.coordinator import Coordinator
from theo.backends.claude import ClaudeBackend
from theo.backends.codex import CodexBackend
from theo.backends.policy import inspect_environment
from theo.backends.process import stop_process
from theo.channels.terminal.attachments import attachment_parts
from theo.config import Settings
from theo.delivery.ledger import Delivery
from theo.domain import Denied, uid
from theo.storage import Database
from theo.tools.broker import ToolBroker
from theo.work.jobs import Jobs


def require_local_live(live: bool) -> None:
    if not live:
        raise Denied("Native subscription tests require explicit --live")
    if any(
        os.environ.get(key, "").lower() not in ("", "0", "false")
        for key in ("CI", "GITHUB_ACTIONS", "THEO_TEST_OFFLINE")
    ):
        raise Denied("Live native tests are local-only and cannot run in CI/offline mode")


def native_environment() -> dict[str, str]:
    forbidden = inspect_environment(dict(os.environ))
    if forbidden:
        raise Denied(
            "Remove API/custom-route environment keys before testing: " + ", ".join(forbidden)
        )
    return {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "HOME",
            "USER",
            "LOGNAME",
            "PATH",
            "LANG",
            "LC_ALL",
            "TERM",
            "TMPDIR",
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "XDG_CONFIG_HOME",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
    }


async def subscription_status(backend: str, environment: dict[str, str], workspace: Path) -> str:
    command = ["codex", "login", "status"] if backend == "codex" else ["claude", "auth", "status"]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=workspace,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), 15)
    finally:
        await stop_process(process)
    if backend == "codex":
        included = process.returncode == 0 and b"Logged in using ChatGPT" in out + err
    else:
        status = json.loads(out)
        included = (
            process.returncode == 0
            and status.get("loggedIn") is True
            and status.get("authMethod") == "claude.ai"
        )
    if not included:
        raise Denied(f"Sign in to {backend} with the native subscription login before testing")
    return "native_subscription"


class ProbeBroker(ToolBroker):
    def grant(self, context):
        return super().grant(
            context.model_copy(update={"tools": frozenset({"remember", "recall"})})
        )


def local_launch(settings, protected_root, workspace, command):
    # The production adapter still supplies its native permission policy. This
    # harness does not attest an isolated deployment or modify launch_options.
    return command, {}


async def run(args) -> int:
    require_local_live(args.live)
    environment = native_environment()
    report = {
        "scope": "local native adapter + coordinator + MCP + SQLite + local delivery",
        "backend": args.backend,
        "model": args.model,
        "python": platform.python_version(),
        "telegram_tested": False,
        "deployment_qualification_tested": False,
        "cases": [],
        "passed": False,
        "started_at": datetime.now(UTC).isoformat(),
    }

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    with tempfile.TemporaryDirectory(prefix="theo-native-", dir="/tmp") as directory:
        root = Path(directory)
        await subscription_status(args.backend, environment, root)
        settings = Settings(primary_backend=args.backend, primary_model=args.model)
        db = Database(root / "data")
        await db.initialize()
        broker = ProbeBroker(db, settings)
        socket_path = root / "broker.sock"
        base = CodexBackend if args.backend == "codex" else ClaudeBackend

        class LocalSubscriptionBackend(base):
            async def preparation(self, request):
                return environment, {"pool_id": "local-e2e-only"}

        coordinator = Coordinator(
            db,
            settings,
            broker,
            socket_path,
            factory=lambda name: LocalSubscriptionBackend(db, settings),
        )
        report["runtime_version"] = await LocalSubscriptionBackend(db, settings).version()
        marker = "theo-e2e-" + uid()[:8]
        secret = "violet-otter-" + uid()[:8]
        document_secret = "amber-lark-" + uid()[:8]
        document = root / "synthetic-note.txt"
        document.write_text("The synthetic document code is " + document_secret)
        cases = [
            ("round_trip", f"Reply with exactly {marker} and nothing else.", marker, None, []),
            (
                "remember",
                f"Use remember exactly once to save this synthetic fact: project {marker} has code {secret}. Then reply SAVED.",
                "SAVED",
                "remember",
                [],
            ),
            # A fresh conversation forces a tool-based recall across sessions.
            (
                "recall",
                f"This is a tool integration test. You MUST call recall with query '{marker}', even if the code is already in recalled context. Return the project code from that tool result exactly; no other text.",
                secret,
                "recall",
                [],
            ),
            (
                "document",
                "Read the attached text document. Return its synthetic document code exactly; no other text.",
                document_secret,
                None,
                [document],
            ),
        ]
        try:
            await broker.listen(socket_path)
            with patch("theo.execution.isolation.launch_options", local_launch):
                for name, prompt, expected, tool, paths in cases:
                    conversation = await db.conversation("owner", "local", "test-" + name)
                    parts = await attachment_parts(db, settings, paths)
                    jobs = Jobs(db, "owner")
                    job_id = await jobs.ingest(conversation, "local", uid(), {}, prompt, parts)
                    job = await jobs.claim("interactive", "native-local-test")
                    assert job and job["id"] == job_id
                    async with asyncio.timeout(args.timeout):
                        await coordinator.run_job(job)
                    deliveries = []

                    async def sink(operation, payload, deliveries=deliveries):
                        deliveries.append(payload["text"])
                        return {"message_id": uid(), "channel": "local_test_sink"}

                    while await Delivery(db, settings).dispatch_one(sink):
                        pass
                    result = await db.one("SELECT * FROM runs WHERE job_id=?", (job_id,))
                    assert result
                    events = await db.read(
                        "SELECT kind FROM run_events WHERE run_id=?", (result["id"],)
                    )
                    tool_messages = await db.read(
                        "SELECT content FROM messages WHERE run_id=? AND role='tool'",
                        (result["id"],),
                    )
                    tools = [json.loads(row["content"]) for row in tool_messages]
                    action = await db.one("SELECT id,status FROM actions WHERE job_id=?", (job_id,))
                    receipts = await db.read(
                        "SELECT o.attempts FROM outbox o JOIN delivery_receipts r ON r.delivery_id=o.id WHERE o.action_id=?",
                        (action["id"] if action else "",),
                    )
                    checks = {
                        "completed": result["status"] == "completed",
                        "canonical_context": bool(result["context_id"]),
                        "single_terminal": sum(event["kind"] == "terminal" for event in events)
                        == 1,
                        "answer": expected in (result["output"] or ""),
                        "delivered": len(deliveries) == 1 and expected in deliveries[0],
                        "receipt": bool(
                            action
                            and action["status"] == "succeeded"
                            and len(receipts) == 1
                            and receipts[0]["attempts"] == 1
                        ),
                        "tool": tool is None
                        or any(
                            item["tool"] == tool and item["result"]["status"] in ("committed", "ok")
                            for item in tools
                        ),
                    }
                    if tool == "remember":
                        memories = await db.read("SELECT body FROM memory_revisions")
                        checks["persisted"] = sum(secret in row["body"] for row in memories) == 1
                    if tool == "recall":
                        checks["retrieved"] = any(
                            item["tool"] == "recall" and secret in json.dumps(item["result"])
                            for item in tools
                        )
                    case = {
                        "name": name,
                        "checks": checks,
                        "passed": all(checks.values()),
                        "output": result["output"],
                        "error": result["error"],
                    }
                    report["cases"].append(case)
                    save()
                    print(json.dumps(case), flush=True)
                    if not case["passed"]:
                        break
        except Exception as exc:
            report["error"] = type(exc).__name__ + ": " + str(exc)
        finally:
            await broker.close()
            await db.close()
    report["passed"] = len(report["cases"]) == len(cases) and all(
        case["passed"] for case in report["cases"]
    )
    save()
    return 0 if report["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--backend", choices=("codex", "claude"), required=True)
    parser.add_argument(
        "--model", required=True, help="Exact model included in your native subscription"
    )
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except (Denied, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
