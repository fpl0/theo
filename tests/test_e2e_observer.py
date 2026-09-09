"""Offline checks of live-suite evidence validation, not live E2E evidence."""

import importlib.util
import json
from pathlib import Path

import pytest

from theo.application.coordinator import Coordinator
from theo.delivery.ledger import Delivery
from theo.domain import ExecutionOutcome, Outcome
from theo.tools.broker import ToolBroker
from theo.work.jobs import Jobs

spec = importlib.util.spec_from_file_location(
    "telegram_e2e", Path(__file__).parents[1] / "scripts/telegram_e2e.py"
)
suite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(suite)


async def test_observer_reads_real_schema_and_rejects_canned_reply(db, settings, tmp_path):
    conversation = await db.conversation("owner", "telegram", "123")
    prompt = "[unique-test] echo nonce"
    job_id = await Jobs(db, "owner").ingest(conversation, "telegram", "17", {}, prompt)
    job = await Jobs(db, "owner").claim("interactive", "test")
    broker = ToolBroker(db, settings)
    coordinator = Coordinator(db, settings, broker, tmp_path / "unused")
    # Deliberately counterfeit a canned completion: verifier must reject no model run.
    await coordinator.commit_outcome(
        job, "absent", ExecutionOutcome(status=Outcome.COMPLETED, text="nonce"), None
    )

    async def sink(operation, payload):
        return {"message_id": 42}

    await Delivery(db, settings).dispatch_one(sink)
    observer = suite.Observer(db.root, "owner")
    trace = observer.snapshot(prompt)
    assert trace["job"]["id"] == job_id
    assert len(trace["chunks"]) == 1 and trace["chunks"][0]["remote_id"] == "42"
    assert observer.snapshot("other prompt") is None
    with pytest.raises(suite.CheckFailed, match="native attempt"):
        suite.check_trace(trace, "codex", "included", "nonce")
    await broker.close()


def valid_trace():
    return {
        "job": {"status": "completed"},
        "runs": [
            {"status": "completed", "backend": "codex", "model": "included", "context_id": "ctx"}
        ],
        "terminals": [{"payload": json.dumps({"status": "completed"})}],
        "finals": [{"status": "succeeded"}],
        "chunks": [
            {
                "status": "succeeded",
                "remote_id": "5",
                "attempts": 1,
                "payload": json.dumps({"text": "nonce"}),
            }
        ],
        "tools": [],
    }


@pytest.mark.parametrize(
    "mutation", ["auth", "route", "terminal", "receipt", "retry", "answer", "tool"]
)
def test_trace_does_not_pass_failed_or_incomplete_evidence(mutation):
    trace = valid_trace()
    assert suite.check_trace(trace, "codex", "included", "nonce") == ["nonce"]
    if mutation == "auth":
        trace["runs"][0]["status"] = "waiting_for_auth"
    elif mutation == "route":
        trace["runs"][0]["backend"] = "fake"
    elif mutation == "terminal":
        trace["terminals"] = []
    elif mutation == "receipt":
        trace["chunks"][0]["remote_id"] = None
    elif mutation == "retry":
        trace["chunks"][0]["attempts"] = 2
    elif mutation == "answer":
        trace["chunks"][0]["payload"] = '{"text":"wrong"}'
    with pytest.raises(suite.CheckFailed):
        suite.check_trace(
            trace, "codex", "included", "nonce", "remember" if mutation == "tool" else None
        )
