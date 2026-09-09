from theo.delivery.ledger import Delivery


async def test_cancellation_migration_preserves_existing_queued_actions(db, settings, conversation):
    delivery = Delivery(db, settings)
    action = await delivery.prepare(
        conversation,
        "send_message",
        {"text": "preserved obligation"},
        "old-action",
        durable_obligation=True,
    )
    # Reconstruct the previous schema while preserving a real queued action.
    await db.execute("ALTER TABLE actions DROP COLUMN cancel_requested")
    await db.execute("DELETE FROM schema_migrations WHERE version=5")
    await db.initialize()
    await db.initialize()
    assert (await db.one("SELECT cancel_requested FROM actions WHERE id=?", (action,)))[
        "cancel_requested"
    ] == 0
    sent = []

    async def sender(operation, payload):
        sent.append(payload["text"])
        return {"message_id": "preserved"}

    assert await delivery.dispatch_one(sender)
    assert sent == ["preserved obligation"]
    assert len(await db.read("SELECT * FROM schema_migrations WHERE version=5")) == 1
    assert not await db.read("PRAGMA foreign_key_check")
