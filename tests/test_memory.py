import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from theo.domain import Conflict, Denied
from theo.memory.context import ContextAssembler, estimate
from theo.memory.store import Memory
from theo.operations.export import export_data


async def test_native_instructions_exclude_retrieved_and_user_content(db, conversation):
    await Memory(db, "owner").remember("UNTRUSTED_MEMORY_SENTINEL", source="import", pinned=True)
    context = await ContextAssembler(db, "owner").assemble(conversation, "USER_INPUT_SENTINEL")
    assert "You are Theo" in context["instructions"]
    assert "UNTRUSTED_MEMORY_SENTINEL" not in context["instructions"]
    assert "USER_INPUT_SENTINEL" not in context["instructions"]
    assert "UNTRUSTED_MEMORY_SENTINEL" in context["rendered"]
    assert "USER_INPUT_SENTINEL" in context["rendered"]


async def test_context_anchors_relative_reminders_to_owner_clock(db, conversation, clock):
    await db.execute("UPDATE owners SET timezone='America/New_York' WHERE id='owner'")
    assembler = ContextAssembler(db, "owner")
    context = await assembler.assemble(conversation, "Remind me in 15 minutes")
    marker = "CURRENT TIME (application clock; anchor relative reminders here)\n"
    timing = json.loads(context["rendered"].split(marker)[1].split("\n", 1)[0])
    assert timing["unix_seconds"] == clock()
    assert timing["timezone"] == "America/New_York"
    assert datetime.fromisoformat(timing["utc"]) == datetime.fromtimestamp(clock(), UTC)
    assert datetime.fromisoformat(timing["local"]).timestamp() == clock()
    clock.advance(900)
    refreshed = await assembler.assemble(conversation, "And now?")
    updated = json.loads(refreshed["rendered"].split(marker)[1].split("\n", 1)[0])
    assert updated["unix_seconds"] == timing["unix_seconds"] + 900


async def test_a07_embedding_outage_keeps_fts_pins_and_queued_repair(db, conversation):
    from theo.domain import Unavailable
    from theo.memory.context import ContextAssembler
    from theo.memory.embeddings import Embeddings

    memory = Memory(db, "owner")
    record = await memory.remember("Pinned telescope maintenance", source="fixture", pinned=True)
    with pytest.raises(Unavailable):
        await Embeddings(db, "owner").search("telescope")
    context = await ContextAssembler(db, "owner").assemble(conversation, "telescope")
    assert "Pinned telescope maintenance" in context["rendered"]
    assert await db.one("SELECT memory_id FROM embedding_jobs WHERE memory_id=?", (record,))


async def test_a01_a02_sqlite_authority_after_export_deletion(db, conversation, tmp_path):
    memory = Memory(db, "owner")
    memory_id = await memory.remember(
        "The telescope calibration token is nebula-71", source="owner:fixture", provenance="owner"
    )
    path = await export_data(db, tmp_path / "memories.md", "markdown")
    path.unlink()
    assert (await memory.search("nebula"))[0]["id"] == memory_id
    assert await db.one("SELECT body FROM persona_versions")
    context = await ContextAssembler(db, "owner").assemble(conversation, "telescope")
    assert "nebula-71" in context["rendered"]
    assert context["degraded"] is True


async def test_a03_two_actual_writers_cas(db):
    memory = Memory(db, "owner")
    memory_id = await memory.remember("original", source="owner", provenance="owner")
    results = await asyncio.gather(
        memory.edit(memory_id, 1, "one", source="first"),
        memory.edit(memory_id, 1, "two", source="second"),
        return_exceptions=True,
    )
    assert sorted(type(result).__name__ for result in results) == ["Conflict", "int"]
    assert len(await memory.history(memory_id)) == 2


async def test_a04_a05_model_inference_never_overwrites_and_review_is_exact(db):
    memory = Memory(db, "owner")
    memory_id = await memory.remember("Meeting is Tuesday", source="owner", provenance="owner")
    with pytest.raises(Denied):
        await memory.edit(memory_id, 1, "Actually Wednesday", source="model", actor="model")
    correction = await memory.propose(memory_id, 1, "Meeting is Wednesday", "message:owner")
    assert (await memory.show(memory_id))["body"] == "Meeting is Tuesday"
    await memory.review(correction, True)
    assert (await memory.show(memory_id))["body"] == "Meeting is Wednesday"
    with pytest.raises(Conflict):
        await memory.review(correction, True)


async def test_a06_archive_during_embedding_and_graph(db):
    memory = Memory(db, "owner")
    first = await memory.remember("obsolete comet", source="fixture")
    second = await memory.remember("current star", source="fixture")
    await memory.connect(second, first, "related", "fixture")
    await memory.archive(first)
    assert not await memory.store_embedding(first, 1, [0.5, 0.2], "model", "hash")
    assert not await memory.search("comet")
    assert not await memory.neighbours([second])
    await memory.restore(first)
    assert (await memory.search("comet"))[0]["id"] == first


async def test_supersession_cycles_owner_scope_and_revision_restore(db):
    memory = Memory(db, "owner")
    first = await memory.remember("first", source="fixture")
    second = await memory.remember("second", source="fixture")
    await memory.connect(first, second, "supersedes", "fixture")
    with pytest.raises(Conflict):
        await memory.connect(second, first, "supersedes", "fixture")
    with pytest.raises(Denied):
        await Memory(db, "other").show(first)
    await memory.edit(first, 1, "changed", source="owner")
    await memory.restore(first, 1)
    assert (await memory.show(first))["revision"] == 3
    assert [x["body"] for x in await memory.history(first)] == ["first", "changed", "first"]


async def test_a08_dense_context_counts_input_and_voice(db, conversation):
    memory = Memory(db, "owner")
    for i in range(35):
        await memory.remember(f"star-{i} " + "dense evidence " * 150, source="fixture")
    await db.message("owner", conversation, "user", "star?")
    await db.message("owner", conversation, "assistant", "Old answer")
    await db.message("owner", conversation, "user", "star?")
    context = await ContextAssembler(db, "owner", window=12000).assemble(
        conversation, "star?", "light"
    )
    assert context["estimated_tokens"] == estimate(context["rendered"])
    assert context["estimated_tokens"] < 12000
    assert "Reassess:" in context["rendered"]
    assert context["rendered"].endswith("CURRENT INPUT\nstar?")
    selected = context["sources"]["memory"]
    assert len({x["id"] for x in selected}) == len(selected)
    assert "Answer as Theo" in context["rendered"]


async def test_a09_a10_tool_only_content_survives_checkpoint_and_new_backend(db, conversation):
    await db.message("owner", conversation, "tool", "calibration-secret-8462", source="telescope")
    assembler = ContextAssembler(db, "owner")
    await assembler.checkpoint(conversation)
    for i in range(45):
        await db.message("owner", conversation, "assistant", f"Turn {i}")
    first = await assembler.assemble(conversation, "What was the calibration secret?")
    await db.execute("UPDATE conversations SET backend='grok' WHERE id=?", (conversation,))
    second = await assembler.assemble(conversation, "What was the calibration secret?")
    assert "calibration-secret-8462" in first["rendered"]
    assert "calibration-secret-8462" in second["rendered"]


async def test_fact_revision_invalidates_context_and_preserves_validity(db, conversation, clock):
    memory = Memory(db, "owner")
    fact = await memory.set_fact("plan", "date", "Tuesday", "owner")
    context = await ContextAssembler(db, "owner").assemble(conversation, "plan")
    await memory.set_fact("plan", "date", "Wednesday", "owner", expected=1)
    row = await db.one("SELECT invalidated FROM context_snapshots WHERE id=?", (context["id"],))
    assert row["invalidated"] == 1
    assert (await memory.current_facts())[0]["value"] == "Wednesday"
    with pytest.raises(Conflict):
        await memory.set_fact("plan", "date", "Thursday", "owner", expected=1)
    assert (await db.one("SELECT count(*) n FROM fact_revisions WHERE fact_id=?", (fact,)))[
        "n"
    ] == 2


async def test_a34_failed_statement_rolls_back_and_writer_recovers(db):
    def broken(connection):
        connection.execute(
            "INSERT INTO attention_pins VALUES('pin','owner','body','source',NULL,1)"
        )
        connection.execute("INSERT INTO nonexistent VALUES(1)")

    with pytest.raises(sqlite3.OperationalError):
        await db.write(broken)
    assert not await db.one("SELECT * FROM attention_pins WHERE id='pin'")
    await Memory(db, "owner").remember("still works", source="fixture")
    assert (await db.one("PRAGMA integrity_check"))["integrity_check"] == "ok"


async def test_erase_invalidates_materialized_context(db, conversation):
    memory = Memory(db, "owner")
    memory_id = await memory.remember("erase-me-secret", source="owner")
    await ContextAssembler(db, "owner").assemble(conversation, "erase")
    await memory.erase(memory_id)
    assert not await memory.search("erase")
    assert not await db.read("SELECT * FROM context_snapshots")
