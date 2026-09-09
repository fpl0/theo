"""Live user-client scenarios for Telegram controls, ingestion and topic routing.

Imported by telegram_e2e.py. No Bot API injection or database mutation.
"""

import asyncio
import json
import time
import uuid


async def run_transport(client, bot, observer, settings, args):
    from telegram_e2e import CheckFailed, require, save_report

    report = {
        "scope": "Live Telegram user -> Theo daemon controls and ingestion; no model inference",
        "cases": [],
        "passed": False,
        "not_tested": [
            "native model reasoning",
            "media extraction",
            "media output",
            "client rendering",
            "seven-day soak",
        ],
    }
    tag = "transport-" + uuid.uuid4().hex[:12]

    async def wait_for(query, values=()):
        until = time.monotonic() + args.timeout
        while time.monotonic() < until:
            rows = observer.rows(query, values)
            if rows:
                return rows
            await asyncio.sleep(0.5)
        raise CheckFailed("Timed out waiting for committed transport evidence")

    async def command(text):
        text += " " + tag + "-" + uuid.uuid4().hex[:6]
        sent = await client.send_message(bot, text, parse_mode=None)
        rows = await wait_for(
            "SELECT j.id,a.status,a.request FROM jobs j JOIN actions a ON a.semantic_key='final:'||j.id AND a.owner_id=j.owner_id WHERE j.owner_id=? AND json_extract(j.payload,'$.text')=? AND j.status='completed' AND a.status='succeeded' ORDER BY j.created_at DESC",
            (observer.owner, text),
        )
        until = time.monotonic() + 20
        while time.monotonic() < until:
            received = await client.get_messages(bot, min_id=sent.id, limit=50)
            if any(not m.out for m in received):
                return rows[0]
            await asyncio.sleep(0.5)
        raise CheckFailed("No command response observed in Telegram")

    async def check(name, fn):
        started = time.monotonic()
        case = {"name": name, "passed": False}
        try:
            case["evidence"] = await fn()
            case["passed"] = True
        except Exception as exc:
            case["error"] = str(exc) if isinstance(exc, CheckFailed) else type(exc).__name__
        case["seconds"] = round(time.monotonic() - started, 3)
        report["cases"].append(case)
        save_report(report, args.output)
        require(case["passed"], name + " failed")

    async def controls():
        rows = []
        for name in (
            "help",
            "status",
            "jobs",
            "schedules",
            "models",
            "memory",
            "review",
            "goals",
            "actions",
        ):
            rows.append(await command("/" + name + " " + tag))
        return {"commands": len(rows), "actions": [r["id"] for r in rows]}

    async def edits_and_quotes():
        sent = await client.send_message(bot, tag + " original", parse_mode=None)
        initial = (
            await wait_for(
                "SELECT id,payload FROM jobs WHERE owner_id=? AND json_extract(payload,'$.text')=?",
                (observer.owner, tag + " original"),
            )
        )[0]
        await client.edit_message(bot, sent.id, tag + " edited", parse_mode=None)
        await wait_for(
            "SELECT id FROM jobs WHERE id=? AND json_extract(payload,'$.text')=?",
            (initial["id"], tag + " edited"),
        )
        revisions = observer.rows(
            "SELECT revision FROM telegram_revisions WHERE json_extract(body,'$.text') IN (?,?)",
            (tag + " original", tag + " edited"),
        )
        require(len(revisions) == 2, "Edit did not preserve two immutable versions")
        await client.send_message(bot, tag + " reply", reply_to=sent.id, parse_mode=None)
        reply = (
            await wait_for(
                "SELECT id,payload FROM jobs WHERE owner_id=? AND json_extract(payload,'$.text') LIKE ?",
                (observer.owner, tag + " reply%"),
            )
        )[0]
        require(tag + " edited" in json.loads(reply["payload"])["text"], "Reply context missing")
        for job in (initial, reply):
            await command("/cancel " + job["id"])
        return {"edited_job": initial["id"], "reply_job": reply["id"], "revisions": len(revisions)}

    async def group_topics():
        rows = []
        for destination in settings.get("telegram_destinations", []):
            chat = await client.get_entity(destination["chat_id"])
            text = f"@{bot.username} {tag} topic-{destination.get('topic_id', 0)}"
            await client.send_message(
                chat, text, reply_to=destination.get("topic_id") or None, parse_mode=None
            )
            job = (
                await wait_for(
                    "SELECT j.id,d.chat_id,d.topic_id FROM jobs j JOIN telegram_destinations d ON d.conversation_id=j.conversation_id JOIN telegram_messages t ON t.job_id=j.id WHERE j.owner_id=? AND json_extract(t.body,'$.text')=?",
                    (observer.owner, text),
                )
            )[0]
            require(
                job["chat_id"] == destination["chat_id"]
                and job["topic_id"] == destination.get("topic_id", 0),
                "Incorrect topic routing",
            )
            await command("/cancel " + job["id"])
            rows.append(job)
        require(bool(rows), "No allowed test groups/topics configured")
        return rows

    try:
        await check("host_controls", controls)
        await command("/pause models")
        await check("edited_messages_and_reply_context", edits_and_quotes)
        if settings.get("telegram_destinations"):
            await check("allowed_group_topic_routing", group_topics)
        else:
            report["not_tested"].append("group/topic routing: no configured test destinations")
        report["passed"] = all(case["passed"] for case in report["cases"])
    finally:
        try:
            await command("/resume models")
        except Exception:
            report["passed"] = False
            report["recovery_required"] = "Verify /resume models in the private test chat"
        save_report(report, args.output)
    return 0 if report["passed"] else 1
