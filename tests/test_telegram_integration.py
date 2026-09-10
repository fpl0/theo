"""Behavioral Telegram contracts; no network calls or accounts."""

import json

import pytest
from aiogram.types import Update

from theo.application.commands import ConversationCommands
from theo.channels.telegram.adapter import Telegram
from theo.channels.telegram.controls import TelegramUI
from theo.channels.telegram.rendering import rich_html
from theo.config import Settings
from theo.delivery.ledger import Delivery
from theo.domain import Conflict, Denied, TelegramDestination, ToolContext, encode, uid
from theo.memory.context import ContextAssembler
from theo.memory.store import Memory
from theo.tools.broker import ToolBroker
from theo.tools.registry import REGISTRY
from theo.work.jobs import Jobs


@pytest.fixture
async def telegram(db):
    configured = Settings(
        telegram_owner_id=123,
        telegram_chat_id=123,
        telegram_destinations=(
            TelegramDestination(chat_id=-456, topic_id=7),
            TelegramDestination(chat_id=-456, topic_id=8),
        ),
    )
    client = Telegram(db, configured, "789:TEST_FIXTURE_TOKEN")
    client.username = "theobot"
    yield client
    await client.close()


def message(update=1, text="hello", *, chat=123, topic=None, actor=123, mid=None, **extra):
    return Update.model_validate(
        {
            "update_id": update,
            "message": {
                "message_id": mid or update,
                "date": 1788782400,
                "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup"},
                "message_thread_id": topic,
                "from": {"id": actor, "is_bot": False, "first_name": "Owner"},
                "text": text,
                **extra,
            },
        }
    )


@pytest.mark.parametrize("chat,topic", [(123, None), (-456, 7)])
@pytest.mark.parametrize("real_work", [False, True])
async def test_telegram_status_does_not_count_its_own_command(db, telegram, chat, topic, real_work):
    async def cancel(job_id):
        raise AssertionError("Status must not cancel work")

    conversation = await telegram.state.destination(chat, topic or 0)
    if chat < 0:
        private = await telegram.state.destination(123)
        await Jobs(db, "owner").enqueue(private, "deep_work", {"text": "Private work"}, "private")
    if real_work:
        await Jobs(db, "owner").enqueue(
            conversation, "deep_work", {"text": "Actual pending work"}, "pending"
        )
    commands = ConversationCommands(db, telegram.settings, cancel)
    for update in (1, 2):
        await telegram.ingest(message(update, "/status", chat=chat, topic=topic))
        report = await db.one(
            "SELECT id FROM jobs WHERE semantic_key=?", (f"inbox:telegram:{update}",)
        )
        await commands.process_pending()
        action = await db.one(
            "SELECT request FROM actions WHERE semantic_key=?", (f"final:{report['id']}",)
        )
        text = json.loads(action["request"])["text"]
        assert f"queued: {int(real_work)}" in text.splitlines()
        assert "Background: paused" in text
        assert (await db.one("SELECT status FROM jobs WHERE id=?", (report["id"],)))[
            "status"
        ] == "completed"


async def uncertain_delivery(db, telegram, *, operation="send_message", request=None):
    conversation = await telegram.state.destination(123)
    delivery = Delivery(db, telegram.settings)
    action = await delivery.prepare(
        conversation, operation, request or {"text": "Synthetic uncertain delivery"}, uid()
    )

    async def lost_receipt(operation, payload):
        raise ConnectionResetError("Injected loss after send")

    assert await delivery.dispatch_one(lost_receipt)
    chunk = await db.one("SELECT id FROM outbox WHERE action_id=?", (action,))
    return conversation, delivery, action, chunk["id"]


async def test_owner_can_record_received_message_in_chat_without_resending(db, telegram):
    conversation, delivery, action, chunk = await uncertain_delivery(db, telegram)
    ui = TelegramUI(db, telegram.settings)
    group = await telegram.state.destination(-456, 7)
    with pytest.raises(Denied, match="private"):
        await ui.command(group, f"/delivered {action} {chunk} 501", "group-denied")
    assert await ui.command(conversation, f"/delivered {action} {chunk} 501", "received")
    assert await ui.command(conversation, f"/delivered {action} {chunk} 501", "repeated")
    recorded = await db.one("SELECT status,attempts FROM outbox WHERE id=?", (chunk,))
    assert recorded == {"status": "succeeded", "attempts": 1}
    assert len(await db.read("SELECT * FROM delivery_receipts WHERE delivery_id=?", (chunk,))) == 1
    mapped = await db.one("SELECT action_id FROM telegram_messages WHERE message_id=501")
    assert mapped["action_id"] == action
    with pytest.raises(Denied):
        await ui.command(conversation, f"/delivered {action} {chunk} 502", "changed-confirmation")
    assert await db.one("SELECT id FROM actions WHERE semantic_key='repeated'")


@pytest.mark.parametrize(
    "sender,chat,topic,actor,expected",
    [
        (789, 123, None, 123, "succeeded"),
        (123, 123, None, 123, "uncertain"),
        (789, -456, None, 123, "uncertain"),
        (789, 123, 7, 123, "uncertain"),
        (789, 123, None, 999, "uncertain"),
    ],
)
async def test_reply_confirmation_uses_telegram_identity_through_coordinator(
    db, telegram, tmp_path, sender, chat, topic, actor, expected
):
    from theo.application.coordinator import Coordinator

    _, _, action, chunk = await uncertain_delivery(db, telegram)
    await telegram.ingest(
        message(
            2,
            f"/delivered {action} {chunk}",
            actor=actor,
            reply_to_message={
                "message_id": 501,
                "date": 1788782400,
                "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup"},
                "message_thread_id": topic,
                "from": {"id": sender, "is_bot": sender == 789, "first_name": "Test"},
                "text": "Synthetic uncertain delivery",
            },
        )
    )
    coordinator = Coordinator(
        db, telegram.settings, ToolBroker(db, telegram.settings), tmp_path / "unused.sock"
    )
    await coordinator.commands()
    assert (await db.one("SELECT status FROM actions WHERE id=?", (action,)))["status"] == expected
    assert (await db.one("SELECT attempts FROM outbox WHERE id=?", (chunk,)))["attempts"] == 1


@pytest.mark.parametrize(
    "receipt",
    [
        {"message_id": 1},
        {"message_id": True},
        {"message_id": -1},
        {"message_id": 501, "chat_id": -456},
        {"message_id": 501, "topic_id": 7},
        {"message_id": 501, "messages": [{"message_id": 502}]},
        {"message_id": 501, "messages": [{"message_id": 501, "chat_id": -456}]},
    ],
)
async def test_reconciliation_rejects_wrong_or_conflicting_telegram_identity(db, telegram, receipt):
    await telegram.ingest(message())
    _, delivery, action, chunk = await uncertain_delivery(db, telegram)
    with pytest.raises(Denied):
        await delivery.reconcile(action, receipt=receipt, delivery_id=chunk)
    assert (await db.one("SELECT status FROM outbox WHERE id=?", (chunk,)))["status"] == "uncertain"
    assert not await db.one("SELECT * FROM delivery_receipts WHERE delivery_id=?", (chunk,))


async def test_album_reconciliation_needs_every_unique_message_id(db, telegram):
    conversation, delivery, action, chunk = await uncertain_delivery(
        db,
        telegram,
        operation="send_media_group",
        request={
            "items": [
                {"kind": "photo", "artifact_id": "one"},
                {"kind": "photo", "artifact_id": "two"},
            ]
        },
    )
    ui = TelegramUI(db, telegram.settings)
    with pytest.raises(Denied, match="each delivered item"):
        await ui.command(conversation, f"/delivered {action} {chunk} 601", "missing-item")
    with pytest.raises(ValueError, match="unique"):
        await ui.command(conversation, f"/delivered {action} {chunk} 601,601", "duplicate-item")
    assert await ui.command(conversation, f"/delivered {action} {chunk} 601,602", "album-received")
    assert len(await db.read("SELECT * FROM telegram_messages WHERE action_id=?", (action,))) == 2


async def test_reconciliation_cannot_reuse_successful_chunk_receipt(db, telegram):
    conversation = await telegram.state.destination(123)
    delivery = Delivery(db, telegram.settings)
    action = await delivery.prepare(conversation, "send_message", {"text": "a" * 5000}, "partial")
    calls = 0

    async def send(operation, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"message_id": 701}
        raise ConnectionResetError("Injected loss after second send")

    await delivery.dispatch_one(send)
    await delivery.dispatch_one(send)
    chunk = await db.one(
        "SELECT id FROM outbox WHERE action_id=? AND status='uncertain'", (action,)
    )
    with pytest.raises(Denied, match="different chunk"):
        await delivery.reconcile(action, delivery_id=chunk["id"], receipt={"message_id": 701})
    await delivery.reconcile(action, delivery_id=chunk["id"], receipt={"message_id": 702})
    assert (await db.one("SELECT status FROM actions WHERE id=?", (action,)))[
        "status"
    ] == "succeeded"
    assert calls == 2


async def test_receipt_can_be_normalized_after_restart(db, telegram):
    await telegram.state.receive(message())
    assert not await db.one("SELECT * FROM jobs")
    await telegram.process_pending()
    await telegram.ingest(message())
    assert len(await db.read("SELECT * FROM jobs")) == 1
    assert (await db.one("SELECT status FROM telegram_events"))["status"] == "done"
    assert (await db.one("SELECT canonical_id FROM telegram_messages"))["canonical_id"]


async def test_edits_replace_queued_work_and_preserve_revisions(db, telegram):
    await telegram.ingest(message(text="old"))
    edited = message(update=2, text="new", mid=1, edit_date=1788782500).model_dump(
        mode="json", exclude_none=True
    )
    edited["edited_message"] = edited.pop("message")
    await telegram.ingest(Update.model_validate(edited))
    assert len(await db.read("SELECT * FROM jobs")) == 1
    assert json.loads((await db.one("SELECT payload FROM jobs"))["payload"])["text"] == "new"
    assert len(await db.read("SELECT * FROM telegram_revisions")) == 2
    await telegram.ingest(Update.model_validate(edited))
    assert len(await db.read("SELECT * FROM telegram_revisions")) == 2


async def test_running_edit_fences_worker_and_does_not_repeat_effects(db, telegram):
    await telegram.ingest(message())
    job = await Jobs(db, "owner").claim("interactive", "fixture")
    run = uid()
    await db.execute(
        "INSERT INTO runs(id,owner_id,job_id,generation,backend,model,status,started_at) VALUES(?,?,?,?,?,?,?,?)",
        (run, "owner", job["id"], job["generation"], "codex", "fixture", "running", db.clock()),
    )
    await db.execute(
        "INSERT INTO tool_receipts VALUES(?,?,?,?,?)",
        ("owner", job["id"], "effect", '{"status":"committed"}', db.clock()),
    )
    edited = message(update=2, mid=1, text="corrected", edit_date=1788782500).model_dump(
        mode="json", exclude_none=True
    )
    edited["edited_message"] = edited.pop("message")
    await telegram.ingest(Update.model_validate(edited))
    with pytest.raises(Denied):
        await db.write(lambda conn: Jobs(db, "owner").check(conn, job["id"], job["generation"]))
    replacement = await db.one("SELECT * FROM jobs WHERE status='queued'")
    assert json.loads(replacement["payload"])["correction_effects"]
    assert "PRIOR EFFECTS" in replacement["payload"]


async def test_album_collects_once_across_consumer_restart(db, telegram, clock):
    for mid in (1, 2):
        await telegram.ingest(
            message(
                mid,
                text=None,
                media_group_id="album",
                photo=[{"file_id": str(mid), "file_unique_id": str(mid), "width": 2, "height": 2}],
            )
        )
    assert not await db.one("SELECT * FROM jobs")
    clock.advance(1.1)
    await telegram.process_pending()
    assert len(await db.read("SELECT * FROM jobs")) == 1
    payload = json.loads((await db.one("SELECT payload FROM jobs"))["payload"])
    assert len(payload["parts"]) == 2
    assert len(await db.read("SELECT * FROM telegram_messages")) == 2
    await telegram.process_pending()
    assert len(await db.read("SELECT * FROM jobs")) == 1


async def test_group_invocation_and_topic_allowlist(db, telegram):
    for update in (
        message(1, chat=-456, topic=7),
        message(2, "/status", chat=-456, topic=9),
        message(3, "/status", chat=-456, topic=7, actor=999),
    ):
        await telegram.ingest(update)
    assert not await db.one("SELECT * FROM jobs")
    await telegram.ingest(message(4, "@theobot help", chat=-456, topic=7))
    await telegram.ingest(message(5, "/status", chat=-456, topic=8))
    jobs = await db.read("SELECT conversation_id FROM jobs")
    assert len(jobs) == 2 and jobs[0] != jobs[1]


async def test_group_context_excludes_all_private_sources(db, telegram):
    group = await telegram.state.destination(-456, 7)
    other = await telegram.state.destination(-456, 8)
    await Memory(db, "owner").remember("SECRET private memory", source="owner", pinned=True)
    await Memory(db, "owner").set_fact("SECRET", "private fact", "SECRET fact", "owner")
    await Memory(db, "owner", other).remember("SECRET other topic", source="owner", pinned=True)
    own = await Memory(db, "owner", group).remember(
        "visible local memory", source="owner", pinned=True
    )
    await db.execute(
        "INSERT INTO attention_pins VALUES(?,?,?,?,?,?)",
        (uid(), "owner", "SECRET pin", "owner", None, db.clock()),
    )
    context = await ContextAssembler(db, "owner").assemble(group, "SECRET visible")
    assert "SECRET private memory" not in context["rendered"]
    assert "SECRET fact" not in context["rendered"]
    assert "SECRET other topic" not in context["rendered"]
    assert "SECRET pin" not in context["rendered"]
    assert own in encode(context["sources"])


async def test_group_tool_retrieval_and_mutation_boundaries(db, telegram, tmp_path):
    private = await telegram.state.destination(123)
    group = await telegram.state.destination(-456, 7)
    private_memory = await Memory(db, "owner").remember("private secret", source="owner")
    private_correction = await Memory(db, "owner").propose(
        private_memory, 1, "private correction secret", "owner"
    )
    shared_memory = await Memory(db, "owner", group).remember("shared memory", source="owner")
    shared_correction = await Memory(db, "owner", group).propose(
        shared_memory, 1, "shared correction", "owner"
    )
    private_action = await Delivery(db, telegram.settings).prepare(
        private, "send_message", {"text": "SECRET"}, "private-action"
    )
    await Jobs(db, "owner").enqueue(
        group, "conversation", {"text": "hello"}, "group-job", lane="interactive"
    )
    job = await Jobs(db, "owner").claim("interactive", "fixture")
    context = ToolContext(
        owner_id="owner",
        conversation_id=group,
        job_id=job["id"],
        run_id=uid(),
        generation=job["generation"],
        workspace=tmp_path,
        tools=frozenset(REGISTRY),
    )
    broker = ToolBroker(db, telegram.settings)
    token = broker.grant(context)
    for name, args in (
        ("memory_history", {"id": private_memory}),
        ("forget", {"id": private_memory}),
        ("action_status", {"id": private_action}),
        ("get_cost_report", {}),
        ("command_run", {"argv": ["pwd"]}),
    ):
        assert (await broker.call(token, name, args)).status == "denied"
    assert not (await broker.call(token, "recall", {"query": "private secret"})).data
    corrections = await broker.call(token, "review_corrections", {})
    assert corrections.status == "ok"
    assert [row["id"] for row in corrections.data] == [shared_correction]
    assert private_correction not in encode(corrections.model_dump())
    assert "private correction secret" not in encode(
        await db.read("SELECT content FROM messages WHERE conversation_id=?", (group,))
    )
    assert (await Memory(db, "owner").show(private_memory))["status"] == "active"


async def test_delivery_keeps_topic_and_reply_across_chunks(db, telegram):
    await telegram.ingest(message(1, "@theobot long", chat=-456, topic=7))
    job = await db.one("SELECT * FROM jobs")
    action = await Delivery(db, telegram.settings).prepare(
        job["conversation_id"], "send_message", {"text": "x" * 5000}, "long", job_id=job["id"]
    )
    calls = []

    async def send(operation, payload):
        calls.append(payload)
        return {"message_id": 10 + len(calls), "chat_id": -456}

    while await Delivery(db, telegram.settings).dispatch_one(send):
        pass
    assert len(calls) == 2
    assert all(x["_telegram"]["topic_id"] == 7 and x["reply_to"] == 1 for x in calls)
    assert (await db.one("SELECT status FROM actions WHERE id=?", (action,)))[
        "status"
    ] == "succeeded"


@pytest.mark.parametrize(
    "operation,outbound_request,newer,expected",
    [
        ("send_message", {}, False, None),
        ("send_message", {}, True, 20),
        ("reply", {}, False, 20),
        ("reply", {"reply_to": 10}, False, 10),
        ("send_message", {"reply_to": 10}, True, 10),
        ("send_message", {"reply_to": None}, True, None),
    ],
)
async def test_private_answer_quotes_only_when_reference_helps(
    db, telegram, operation, outbound_request, newer, expected
):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    await telegram.ingest(message(10, "Earlier question"))
    await telegram.ingest(message(20, "How are you?"))
    job = await db.one("SELECT * FROM jobs WHERE semantic_key='inbox:telegram:20'")
    # Tool evidence and other conversations must not make a direct answer look delayed.
    await db.write(
        lambda conn: db.append_message(
            conn, "owner", job["conversation_id"], "tool", "Synthetic result", db.clock()
        )
    )
    await telegram.ingest(message(21, "@theobot Other topic", chat=-456, topic=7))
    if newer:
        await telegram.ingest(message(30, "A different question"))
    delivery = Delivery(db, telegram.settings)
    action = await delivery.prepare(
        job["conversation_id"],
        operation,
        {"text": "I'm here and glad to be talking with you.", **outbound_request},
        "private-answer",
        job_id=job["id"],
    )
    calls = []

    class FakeBot:
        async def send_message(self, **kwargs):
            raise AssertionError("Unexpected fallback")

        async def send_rich_message(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                message_id=40, chat=SimpleNamespace(id=123), date=datetime.now(UTC)
            )

    original = telegram.bot
    telegram.bot = FakeBot()
    try:
        assert await delivery.dispatch_one(telegram.send)
    finally:
        telegram.bot = original
    assert len(calls) == 1 and calls[0]["chat_id"] == 123
    reference = calls[0].get("reply_parameters")
    assert (reference.message_id if reference else None) == expected
    if expected is None:
        assert "reply_parameters" not in calls[0]
    assert (await db.one("SELECT status FROM actions WHERE id=?", (action,)))[
        "status"
    ] == "succeeded"


@pytest.mark.parametrize(
    "operation,outbound_request",
    [
        ("send_message", {"text": "x" * 5000}),
        ("send_photo", {"artifact_id": "synthetic", "caption": "x" * 5000}),
        ("send_document", {"artifact_id": "synthetic"}),
        ("send_voice", {"artifact_id": "synthetic"}),
        ("send_video", {"artifact_id": "synthetic"}),
        ("send_audio", {"artifact_id": "synthetic"}),
        ("send_animation", {"artifact_id": "synthetic"}),
        ("send_media_group", {"items": []}),
    ],
)
async def test_private_delivery_chunks_do_not_automatically_quote_input(
    db, telegram, operation, outbound_request
):
    quoted = message(1, "Earlier context").message.model_dump(mode="json", exclude_none=True)
    await telegram.ingest(message(2, "What about this?", reply_to_message=quoted))
    job = await db.one("SELECT * FROM jobs")
    assert "Earlier context" in json.loads(job["payload"])["text"]
    action = await Delivery(db, telegram.settings).prepare(
        job["conversation_id"], operation, outbound_request, "private-output", job_id=job["id"]
    )
    chunks = await db.read("SELECT payload FROM outbox WHERE action_id=?", (action,))
    assert chunks
    assert all("reply_to" not in json.loads(chunk["payload"]) for chunk in chunks)


@pytest.mark.parametrize("initial_reply", [False, True])
async def test_reply_choice_is_stable_on_replay_after_new_input(db, telegram, initial_reply):
    await telegram.ingest(message(1, "First question"))
    job = await db.one("SELECT * FROM jobs")
    if initial_reply:
        await telegram.ingest(message(2, "Second question"))
    delivery = Delivery(db, telegram.settings)
    action = await delivery.prepare(
        job["conversation_id"], "send_message", {"text": "Answer"}, "answer", job_id=job["id"]
    )
    before = await db.one("SELECT request,request_hash FROM actions WHERE id=?", (action,))
    assert json.loads(before["request"]).get("reply_to") == (1 if initial_reply else None)
    await telegram.ingest(message(3, "Another question"))
    assert action == await delivery.prepare(
        job["conversation_id"], "send_message", {"text": "Answer"}, "answer", job_id=job["id"]
    )
    assert await db.one("SELECT request,request_hash FROM actions WHERE id=?", (action,)) == before
    assert len(await db.read("SELECT id FROM outbox WHERE action_id=?", (action,))) == 1
    for changed in ({"text": "Changed answer"}, {"text": "Answer", "reply_to": 3}):
        with pytest.raises(Conflict):
            await delivery.prepare(
                job["conversation_id"], "send_message", changed, "answer", job_id=job["id"]
            )


async def test_message_tools_offer_plain_answers_and_explicit_references(db, telegram, tmp_path):
    await telegram.ingest(message(1, "Earlier question"))
    job = await Jobs(db, "owner").claim("interactive", "fixture")
    broker = ToolBroker(db, telegram.settings)
    token = broker.grant(
        ToolContext(
            owner_id="owner",
            conversation_id=job["conversation_id"],
            job_id=job["id"],
            run_id=uid(),
            generation=job["generation"],
            workspace=tmp_path,
            tools=frozenset(REGISTRY),
        )
    )
    plain = await broker.call(token, "send_message", {"text": "Direct answer"})
    await telegram.ingest(message(2, "New question"))
    replay = await broker.call(token, "send_message", {"text": "Direct answer"})
    assert replay == plain
    quoted = await broker.call(
        token, "reply", {"text": "About the earlier question", "reply_to": 1}
    )
    assert plain.status == quoted.status == "ready"
    plain_request = await db.one("SELECT request FROM actions WHERE id=?", (plain.action_id,))
    quoted_request = await db.one("SELECT request FROM actions WHERE id=?", (quoted.action_id,))
    assert "reply_to" not in json.loads(plain_request["request"])
    assert json.loads(quoted_request["request"])["reply_to"] == 1


async def test_callback_is_bound_to_message_and_is_single_use(db, telegram):
    conv = await telegram.state.destination(123)
    ui = TelegramUI(db, telegram.settings)
    action = await ui.card(conv, "Choose", "card", [("Models", "view", {"command": "/models"})])

    async def send(operation, payload):
        return {"message_id": 50}

    await Delivery(db, telegram.settings).dispatch_one(send)
    token = (await db.one("SELECT token FROM telegram_callbacks WHERE action_id=?", (action,)))[
        "token"
    ]
    assert "another message" in await ui.callback(conv, 51, "ui:" + token)
    assert await ui.callback(conv, 50, "ui:" + token) == "Decision recorded."
    count = len(await db.read("SELECT * FROM actions"))
    assert await ui.callback(conv, 50, "ui:" + token) == "Decision recorded."
    assert len(await db.read("SELECT * FROM actions")) == count


async def test_group_actions_reviewed_in_private_chat(db, telegram):
    group = await telegram.state.destination(-456, 7)
    private = await telegram.state.destination(123)
    action = await Delivery(db, telegram.settings).prepare(
        group, "delete_message", {"message_id": 1}, "delete", require_approval=True
    )
    ui = TelegramUI(db, telegram.settings)
    await ui.reviews()
    card = await db.one("SELECT * FROM actions WHERE semantic_key LIKE 'review:%'")
    assert card["conversation_id"] == private
    card_text = json.loads(card["request"])["text"]
    assert "Destination: Chat -456, topic 7" in card_text
    assert '"message_id": 1' in card_text
    assert "conversation_id" not in card_text and "_telegram" not in card_text

    async def send(operation, payload):
        return {"message_id": 60}

    await Delivery(db, telegram.settings).dispatch_one(send)
    details = await db.one("SELECT * FROM telegram_callbacks WHERE operation='inspect_action'")
    assert await ui.callback(private, 60, "ui:" + details["token"]) == "Decision recorded."
    assert (await db.one("SELECT status FROM actions WHERE id=?", (action,)))[
        "status"
    ] == "awaiting_approval"
    callback = await db.one("SELECT * FROM telegram_callbacks WHERE operation='approve'")
    assert await ui.callback(private, 60, "ui:" + callback["token"]) == "Decision recorded."
    assert (await db.one("SELECT status FROM actions WHERE id=?", (action,)))["status"] == "ready"


def test_renderer_never_interprets_model_controls():
    result = rich_html('**bold**\n```\n<&>\n```\n<tg-button type="callback">evil</tg-button>')
    assert "<b>bold</b>" in result
    assert "&lt;tg-button" in result
    assert "<pre>&lt;&amp;&gt;</pre>" in result
    assert rich_html("Status\nready\n\nNext") == "Status<br>ready<br><br>Next"


@pytest.mark.parametrize("chat,topic", [(123, None), (-456, 7)])
async def test_typing_has_no_draft_or_message_and_stops_with_generation(
    db, telegram, clock, chat, topic
):
    await telegram.ingest(message(text="@theobot hello", chat=chat, topic=topic))
    job = await Jobs(db, "owner").claim("interactive", "test")
    calls = []

    class FakeBot:
        async def send_chat_action(self, **kwargs):
            calls.append(kwargs)

        async def send_message_draft(self, **kwargs):
            pytest.fail("Partial answers must never replace Telegram bubbles")

        async def send_message(self, **kwargs):
            pytest.fail("Typing must not send messages outside the delivery ledger")

    original = telegram.bot
    telegram.bot = FakeBot()
    try:
        await telegram.typing(job)
        await telegram.typing(job)
        assert len(calls) == 1
        assert calls[0]["action"] == "typing"
        assert calls[0]["chat_id"] == chat
        assert calls[0]["message_thread_id"] == topic
        clock.advance(4)
        await telegram.typing(job)
        assert len(calls) == 2
        stale = {**job, "generation": job["generation"] + 1}
        clock.advance(4)
        await telegram.typing(stale)
        assert len(calls) == 2
        await Jobs(db, "owner").cancel(job["id"])
        await telegram.typing(job)
        assert len(calls) == 2
        assert not await db.read("SELECT * FROM telegram_previews")
        await telegram.end_preview(job["id"])
        assert not telegram._preview_at
    finally:
        telegram.bot = original


@pytest.mark.parametrize("kind", ["deep_work", "proactive_scan", "reflection", "reminder"])
async def test_background_work_does_not_keep_the_conversation_typing(db, telegram, kind):
    conversation = await telegram.state.destination(123)
    job_id = await Jobs(db, "owner").enqueue(conversation, kind, {"text": "Internal work"}, kind)
    job = await db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    await telegram.typing(job)
    assert not telegram._preview_at
    assert not await db.read("SELECT * FROM telegram_previews")


async def test_streamed_backend_text_produces_one_stable_final_message(db, telegram, tmp_path):
    from theo.application.coordinator import Coordinator
    from theo.backends.base import NativeBackend
    from theo.domain import ExecutionOutcome, Outcome

    class Backend(NativeBackend):
        async def execute(self, request, emit):
            await emit("text_delta", {"text": "Partial thought that must not appear"})
            await emit("text_delta", {"text": "A second draft that replaces the first"})
            return ExecutionOutcome(status=Outcome.COMPLETED, text="Found it. The notes are ready.")

    settings = telegram.settings.model_copy(
        update={"primary_backend": "claude", "primary_model": "fixture"}
    )
    broker = ToolBroker(db, settings)
    coordinator = Coordinator(
        db,
        settings,
        broker,
        tmp_path / "socket",
        factory=lambda name: Backend(db, settings),
        telegram=telegram,
    )
    original = telegram.bot
    calls = []

    class FakeBot:
        async def send_chat_action(self, **kwargs):
            calls.append(("typing", kwargs))

        async def send_message_draft(self, **kwargs):
            pytest.fail("Text deltas must not reach Telegram")

    telegram.bot = FakeBot()
    try:
        await telegram.ingest(message())
        job = await Jobs(db, "owner").claim("interactive", "fixture")
        await coordinator.run_job(job)
        assert calls and all(kind == "typing" for kind, _ in calls)
        sent = []

        async def sender(operation, payload):
            sent.append((operation, payload["text"]))
            return {"message_id": 201}

        assert await Delivery(db, settings).dispatch_one(sender)
        assert not await Delivery(db, settings).dispatch_one(sender)
        assert sent == [("send_message", "Found it. The notes are ready.")]
        assert not await db.read("SELECT * FROM telegram_previews")
        history = await db.read("SELECT content FROM messages WHERE role='assistant'")
        assert history == [{"content": "Found it. The notes are ready."}]
        assert "Partial thought" not in str(await db.read("SELECT payload FROM run_events"))
    finally:
        telegram.bot = original
        await broker.close()


async def test_bad_update_does_not_block_later_work(db, telegram, clock, monkeypatch):
    original = telegram.state.message

    async def normalize(update, username):
        if update.update_id == 1:
            raise ValueError("poison update")
        return await original(update, username)

    monkeypatch.setattr(telegram.state, "message", normalize)
    await telegram.state.receive(message(1))
    await telegram.state.receive(message(2, "healthy"))
    for _ in range(5):
        await telegram.process_pending()
        clock.advance(61)
    assert (await db.one("SELECT status FROM telegram_events WHERE update_id=1"))[
        "status"
    ] == "failed"
    assert (await db.one("SELECT status FROM telegram_events WHERE update_id=2"))[
        "status"
    ] == "done"
    assert len(await db.read("SELECT * FROM jobs")) == 1


@pytest.mark.parametrize(
    "content",
    [
        {"text": "original"},
        {"caption": "original"},
        {"rich_message": {"blocks": [{"type": "paragraph", "text": "original"}]}},
    ],
)
async def test_reply_keeps_quote_as_evidence(db, telegram, content):
    reply = message(1, None).message.model_dump(mode="json", exclude_none=True)
    reply.update(content)
    await telegram.ingest(message(2, "What about this?", reply_to_message=reply))
    payload = json.loads((await db.one("SELECT payload FROM jobs"))["payload"])
    assert "original" in payload["text"]
    assert "untrusted evidence" in payload["text"]
    assert payload["reply_to"] == 2


async def test_callback_expiry_cannot_change_state(db, telegram, clock):
    conv = await telegram.state.destination(123)
    ui = TelegramUI(db, telegram.settings)
    await ui.card(
        conv,
        "route",
        "expired-card",
        [("Choose", "route", {"backend": "codex", "model": "fixture"})],
    )

    async def send(operation, payload):
        return {"message_id": 9}

    await Delivery(db, telegram.settings).dispatch_one(send)
    row = await db.one("SELECT * FROM telegram_callbacks")
    clock.advance(3601)
    assert "expired" in await ui.callback(conv, 9, "ui:" + row["token"])
    assert (await db.one("SELECT model FROM conversations WHERE id=?", (conv,)))["model"] is None


async def test_reaction_counts_and_poll_answers_are_attributed(db, telegram):
    conv = await telegram.state.destination(123)
    await telegram.ingest(message(text="Create a test poll"))
    job = await Jobs(db, "owner").claim("interactive", "fixture")
    run = uid()
    await db.execute(
        "INSERT INTO runs(id,owner_id,job_id,generation,backend,model,status,started_at) VALUES(?,?,?,?,?,?,?,?)",
        (run, "owner", job["id"], job["generation"], "codex", "fixture", "running", db.clock()),
    )
    delivery = Delivery(db, telegram.settings)
    action = await delivery.prepare(
        conv,
        "send_poll",
        {"question": "Choose", "options": ["A", "B"]},
        "poll",
        job_id=job["id"],
        run_id=run,
        generation=job["generation"],
        role="progress",
    )

    async def send(operation, payload):
        return {"message_id": 42, "poll": {"id": "test-poll"}}

    await delivery.dispatch_one(send)
    await telegram.ingest(
        Update.model_validate(
            {
                "update_id": 10,
                "message_reaction_count": {
                    "chat": {"id": 123, "type": "private"},
                    "message_id": 42,
                    "date": 1788782400,
                    "reactions": [{"type": {"type": "emoji", "emoji": "👍"}, "total_count": 2}],
                },
            }
        )
    )
    await telegram.ingest(
        Update.model_validate(
            {
                "update_id": 11,
                "poll_answer": {
                    "poll_id": "test-poll",
                    "user": {"id": 123, "is_bot": False, "first_name": "Owner"},
                    "option_ids": [1],
                    "option_persistent_ids": ["option-b"],
                },
            }
        )
    )
    rows = await db.read("SELECT * FROM feedback ORDER BY created_at,id")
    assert {r["kind"] for r in rows} == {"reaction_count", "poll_answer"}
    assert all(json.loads(r["body"])["conversation_id"] == conv for r in rows)
    assert all(r["action_id"] == action and r["run_id"] == run for r in rows)
    assert all(json.loads(r["body"])["message_id"] == 42 for r in rows)
    for update_id, choices in ((12, []), (13, [0])):
        update = Update.model_validate(
            {
                "update_id": update_id,
                "poll_answer": {
                    "poll_id": "test-poll",
                    "user": {"id": 123, "is_bot": False, "first_name": "Owner"},
                    "option_ids": choices,
                    "option_persistent_ids": ["option-a"] if choices else [],
                },
            }
        )
        await telegram.ingest(update)
        await telegram.ingest(update)
    answers = await db.read(
        "SELECT body,action_id,run_id FROM feedback WHERE kind='poll_answer' ORDER BY id"
    )
    assert [json.loads(row["body"])["option_ids"] for row in answers] == [[1], [], [0]]
    assert all(row["action_id"] == action and row["run_id"] == run for row in answers)
    await telegram.process_pending()
    assert len(await db.read("SELECT * FROM feedback")) == 4


async def test_unknown_media_preserves_original_and_reports_failure(db, telegram):
    original = telegram.bot

    class FakeBot:
        async def download(self, file_id, destination):
            destination.write(b"this is not a JPEG")

    telegram.bot = FakeBot()
    try:
        hydrated = await telegram.hydrate(
            {"kind": "photo", "metadata": {"file_id": "x", "size": 18}}
        )
        assert hydrated.get("artifact_id")
        assert hydrated["metadata"]["state"] == "failed"
        artifact = await db.one(
            "SELECT validated FROM artifacts WHERE id=?", (hydrated["artifact_id"],)
        )
        assert artifact["validated"] == 0
    finally:
        telegram.bot = original


async def test_reminder_command_replay_and_dst(db, telegram):
    from theo.channels.telegram.controls import parse_time

    conv = await telegram.state.destination(123)
    ui = TelegramUI(db, telegram.settings)
    command = "/remind 2026-10-01T10:00:00+01:00 test reminder"
    await ui.command(conv, command, "reminder-command")
    await ui.command(conv, command, "reminder-command")
    assert len(await db.read("SELECT * FROM schedules")) == 1
    with pytest.raises(ValueError):
        parse_time("2026-10-25T01:30:00", "Europe/Dublin")
    with pytest.raises(ValueError):
        parse_time("2026-03-29T01:30:00", "Europe/Dublin")


async def test_migration_preserves_existing_conversation_and_obligation(tmp_path):
    import hashlib
    import sqlite3
    from pathlib import Path

    from theo.storage import Database

    root = tmp_path / "old"
    root.mkdir()
    conn = sqlite3.connect(root / "theo.sqlite3")
    conn.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at REAL NOT NULL)"
    )
    migrations = Path(__file__).parents[1] / "src/theo/migrations"
    for path in sorted(migrations.glob("00[12]_*.sql")):
        source = path.read_text()
        conn.executescript(source)
        conn.execute(
            "INSERT INTO schema_migrations VALUES(?,?,?)",
            (int(path.name[:3]), hashlib.sha256(source.encode()).hexdigest(), 0),
        )
        conn.commit()
    conn.execute("INSERT INTO owners VALUES('owner','Europe/Dublin',0)")
    conn.execute(
        "INSERT INTO conversations(id,owner_id,channel,target) VALUES('existing','owner','telegram','123')"
    )
    conn.execute(
        "INSERT INTO schedules VALUES('obligation','owner','existing','keep me','once',NULL,NULL,'Europe/Dublin',2000000000,1,3600,0)"
    )
    conn.commit()
    conn.close()
    migrated = Database(root)
    try:
        await migrated.initialize()
        assert await migrated.conversation("owner", "telegram", "123") == "existing"
        assert (await migrated.one("SELECT body FROM schedules WHERE id='obligation'"))[
            "body"
        ] == "keep me"
        assert await migrated.one("SELECT version FROM schema_migrations WHERE version=3")
    finally:
        await migrated.close()


async def test_rich_formatting_fallback_requires_definite_rejection(db, telegram):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    original = telegram.bot
    calls = []

    class FakeBot:
        async def send_rich_message(self, **kwargs):
            calls.append("rich")
            raise TelegramBadRequest(
                method=SendMessage(chat_id=123, text="x"), message="can't parse entities"
            )

        async def send_message(self, **kwargs):
            calls.append("plain")
            return SimpleNamespace(
                message_id=5, chat=SimpleNamespace(id=123), date=datetime.now(UTC)
            )

    telegram.bot = FakeBot()
    try:
        result = await telegram.send(
            "send_message",
            {
                "target": "123",
                "text": "**hello**",
                "_telegram": {"bot_id": 789, "chat_id": 123, "topic_id": 0},
            },
        )
        assert result["message_id"] == 5 and calls == ["rich", "plain"]

        async def uncertain(**kwargs):
            calls.append("uncertain")
            raise TimeoutError()

        telegram.bot.send_rich_message = uncertain
        with pytest.raises(TimeoutError):
            await telegram.send(
                "send_message",
                {
                    "target": "123",
                    "text": "hello",
                    "_telegram": {"bot_id": 789, "chat_id": 123, "topic_id": 0},
                },
            )
        assert calls == ["rich", "plain", "uncertain"]
    finally:
        telegram.bot = original


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("send_audio", {"artifact_id": "fixture"}),
        ("send_animation", {"artifact_id": "fixture"}),
        ("send_sticker", {"artifact_id": "fixture"}),
        ("send_video_note", {"artifact_id": "fixture"}),
        ("send_contact", {"phone_number": "+15550000000", "first_name": "Synthetic"}),
        ("send_venue", {"latitude": 0, "longitude": 0, "title": "Synthetic", "address": "Test"}),
        (
            "send_media_group",
            {
                "items": [
                    {"kind": "photo", "artifact_id": "fixture"},
                    {"kind": "photo", "artifact_id": "fixture"},
                ]
            },
        ),
    ],
)
async def test_additional_media_constructs_actual_aiogram_requests(
    db, telegram, operation, payload
):
    import io
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from PIL import Image

    from theo.content.artifacts import Artifacts

    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, "PNG")
    artifact = await Artifacts(db, telegram.settings).store(
        buffer.getvalue(), "fixture.png", "test"
    )
    payload = json.loads(json.dumps(payload).replace('"fixture"', json.dumps(artifact["id"])))
    calls = []
    original = telegram.bot.session

    class Session:
        async def __call__(self, bot, method, **kwargs):
            calls.append(method.model_dump(exclude_none=True))
            reply = SimpleNamespace(
                message_id=7, chat=SimpleNamespace(id=123), date=datetime.now(UTC)
            )
            return [reply, reply] if operation == "send_media_group" else reply

    telegram.bot.session = Session()
    try:
        assert (await telegram.send(operation, {"target": "123", **payload}))["message_id"] == 7
        assert calls[0]["chat_id"] == 123
    finally:
        telegram.bot.session = original
