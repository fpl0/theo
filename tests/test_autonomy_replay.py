import json
from unittest.mock import AsyncMock

import pytest

from theo.application.coordinator import Coordinator
from theo.backends.base import NativeBackend
from theo.config import Settings
from theo.domain import Conflict, ExecutionOutcome, Outcome, encode
from theo.tools.broker import ToolBroker
from theo.work.autonomy import CADENCES, Autonomy
from theo.work.jobs import Jobs


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
async def test_consumed_evidence_survives_legacy_hydration(db, conversation, clock, terminal):
    await db.set_control("owner", "background_paused", "false")
    await db.message("owner", conversation, "user", "Review the supplied notes.")
    clock.advance(121)
    autonomy = Autonomy(db, "owner")
    await autonomy.tick(conversation)
    row = await db.one("SELECT * FROM jobs WHERE kind='proactive_scan'")
    payload = json.loads(row["payload"])
    payload["parts"] = []
    await db.execute(
        "UPDATE jobs SET status=?,payload=? WHERE id=?",
        (terminal, encode(payload), row["id"]),
    )
    clock.advance(CADENCES["proactive_scan"])
    await autonomy.tick(conversation)
    assert len(await db.read("SELECT id FROM jobs WHERE kind='proactive_scan'")) == 1
    await db.message("owner", conversation, "user", "There is another source to review.")
    clock.advance(CADENCES["proactive_scan"])
    await autonomy.tick(conversation)
    assert len(await db.read("SELECT id FROM jobs WHERE kind='proactive_scan'")) == 2
    with pytest.raises(Conflict):
        await Jobs(db, "owner").enqueue(
            conversation,
            row["kind"],
            {"text": "different request"},
            row["semantic_key"],
            origin="autonomous",
        )


async def test_telegram_background_execution_preserves_absent_parts(
    db, conversation, clock, tmp_path
):
    class Backend(NativeBackend):
        async def execute(self, request, emit):
            return ExecutionOutcome(status=Outcome.COMPLETED, text="Internal result.")

    settings = Settings(primary_backend="claude", primary_model="fixture")
    broker = ToolBroker(db, settings)
    telegram = AsyncMock()
    coordinator = Coordinator(
        db,
        settings,
        broker,
        tmp_path / "socket",
        factory=lambda name: Backend(db, settings),
        telegram=telegram,
    )
    payload = {"text": "Review synthetic evidence", "evidence": []}
    job_id = await Jobs(db, "owner").enqueue(
        conversation, "proactive_scan", payload, "synthetic-scan", origin="autonomous"
    )
    try:
        job = await Jobs(db, "owner").claim("background", "fixture")
        await coordinator.run_job(job)
        row = await db.one("SELECT status,payload FROM jobs WHERE id=?", (job_id,))
        assert row["status"] == "completed"
        assert json.loads(row["payload"]) == payload
        telegram.hydrate.assert_not_called()
    finally:
        await broker.close()
