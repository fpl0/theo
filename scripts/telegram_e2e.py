# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = ["telethon==1.44.0"]
# ///
"""Live Telegram user -> Theo daemon -> native model -> Telegram acceptance suite."""

import argparse
import asyncio
import contextlib
import io
import json
import os
import sqlite3
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


class CheckFailed(Exception):
    pass


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


class Observer:
    """Read-only observation. Never inject updates, alter grants, or complete jobs."""

    def __init__(self, root, owner):
        self.path, self.owner = root / "theo.sqlite3", owner

    def rows(self, sql, args=()):
        with contextlib.closing(
            sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            return [dict(row) for row in db.execute(sql, args)]

    def snapshot(self, prompt):
        jobs = self.rows(
            "SELECT j.* FROM jobs j JOIN inbox_updates i "
            "ON j.semantic_key='inbox:telegram:' || i.update_id AND j.owner_id=i.owner_id "
            "WHERE j.owner_id=? AND i.channel='telegram' "
            "AND (json_extract(j.payload,'$.text')=? OR EXISTS(SELECT 1 FROM telegram_messages t WHERE t.job_id=j.id AND json_extract(t.body,'$.text')=?))",
            (self.owner, prompt, prompt),
        )
        if not jobs:
            return None
        require(len(jobs) == 1, "Duplicate jobs for one Telegram input")
        job = jobs[0]
        runs = self.rows("SELECT * FROM runs WHERE job_id=? ORDER BY generation", (job["id"],))
        finals = self.rows(
            "SELECT * FROM actions WHERE owner_id=? AND semantic_key=?",
            (self.owner, "final:" + job["id"]),
        )
        chunks = self.rows(
            "SELECT o.*,r.remote_id FROM outbox o JOIN actions a ON a.id=o.action_id "
            "LEFT JOIN delivery_receipts r ON r.delivery_id=o.id "
            "WHERE a.owner_id=? AND a.semantic_key=? ORDER BY o.ordinal",
            (self.owner, "final:" + job["id"]),
        )
        tools = self.rows(
            "SELECT content FROM messages WHERE run_id IN "
            "(SELECT id FROM runs WHERE job_id=?) AND role='tool'",
            (job["id"],),
        )
        terminals = self.rows(
            "SELECT payload FROM run_events WHERE kind='terminal' AND run_id IN "
            "(SELECT id FROM runs WHERE job_id=?)",
            (job["id"],),
        )
        return {
            "job": job,
            "runs": runs,
            "finals": finals,
            "chunks": chunks,
            "tools": [json.loads(t["content"]) for t in tools],
            "terminals": terminals,
        }


def check_trace(trace, backend, model, expected, tool=None):
    require(trace["job"]["status"] == "completed", "Job did not complete")
    require(len(trace["runs"]) == 1, "Expected one native attempt; retries require inspection")
    run = trace["runs"][0]
    require(run["status"] == "completed", "Native run did not complete")
    require(run["backend"] == backend and run["model"] == model, "Unexpected model route")
    require(bool(run["context_id"]), "Missing canonical context snapshot")
    require(len(trace["terminals"]) == 1, "Expected exactly one model terminal event")
    require(
        json.loads(trace["terminals"][0]["payload"])["status"] == "completed",
        "Native terminal was not successful",
    )
    require(
        len(trace["finals"]) == 1 and trace["finals"][0]["status"] == "succeeded",
        "Final action was not delivered exactly once",
    )
    require(
        bool(trace["chunks"])
        and all(
            c["status"] == "succeeded" and c["remote_id"] and c["attempts"] == 1
            for c in trace["chunks"]
        ),
        "Missing receipt, failed chunk, or repeated delivery attempt",
    )
    texts = [json.loads(c["payload"]).get("text", "") for c in trace["chunks"]]
    require(expected in "".join(texts), "Expected synthetic answer absent from final")
    if tool:
        require(
            any(
                t["tool"] == tool and t["result"]["status"] in {"ok", "committed"}
                for t in trace["tools"]
            ),
            "Required successful model tool call missing: " + tool,
        )
        if tool == "recall":
            require(
                any(
                    t["tool"] == "recall" and expected in json.dumps(t["result"].get("data"))
                    for t in trace["tools"]
                ),
                "Recall result did not contain the saved fact",
            )
    return texts


async def wait_trace(observer, prompt, timeout):
    end = time.monotonic() + timeout
    last = "Telegram input not yet ingested"
    while time.monotonic() < end:
        trace = await asyncio.to_thread(observer.snapshot, prompt)
        if trace:
            status = trace["job"]["status"]
            last = "job=" + status
            require(
                status
                not in {
                    "failed",
                    "cancelled",
                    "interrupted",
                    "uncertain",
                    "waiting_for_auth",
                    "waiting_for_quota",
                    "waiting_for_user",
                },
                "Native pipeline blocked: " + status,
            )
            if trace["finals"]:
                final = trace["finals"][0]["status"]
                last += ", final=" + final
                require(
                    final not in {"failed", "uncertain", "cancelled", "awaiting_approval"},
                    "Final delivery blocked: " + final,
                )
                if status == "completed" and final == "succeeded":
                    return trace
        await asyncio.sleep(1)
    raise CheckFailed("Timed out: " + last)


def save_report(report, output):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    suite = ET.Element(
        "testsuite",
        name="telegram-native-e2e",
        tests=str(len(report["cases"])),
        failures=str(sum(not c["passed"] for c in report["cases"])),
    )
    for case in report["cases"]:
        element = ET.SubElement(suite, "testcase", name=case["name"], time=str(case["seconds"]))
        if not case["passed"]:
            ET.SubElement(element, "failure", message=case["error"])
    ET.ElementTree(suite).write(output / "junit.xml", encoding="unicode", xml_declaration=True)


async def run(args):
    from telethon import TelegramClient

    os.umask(0o077)
    args.session.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(
        "TELEGRAM_API_ID" in os.environ and "TELEGRAM_API_HASH" in os.environ,
        "Set TELEGRAM_API_ID and TELEGRAM_API_HASH for the Telegram user client",
    )
    client = TelegramClient(
        str(args.session),
        int(os.environ["TELEGRAM_API_ID"]),
        os.environ["TELEGRAM_API_HASH"],
        flood_sleep_threshold=0,
        request_retries=1,
        connection_retries=1,
    )
    if args.login_only:
        try:
            await client.start()
            require(not (await client.get_me()).bot, "Log in as a user, not a bot")
            print("Telegram user session saved.")
            return 0
        finally:
            await client.disconnect()
    require(args.live, "Pass --live to send synthetic test messages to your configured Theo bot")
    require(
        args.data_root and args.bot and (args.transport_only or (args.backend and args.model)),
        "--data-root, --bot, --backend and --model are required",
    )
    settings = json.loads((args.data_root / "config.json").read_text())
    observer = Observer(args.data_root, settings.get("owner_id", "owner"))
    controls = {
        r["key"]: r["value"]
        for r in observer.rows("SELECT key,value FROM control WHERE owner_id=?", (observer.owner,))
    }
    require(
        controls.get("quarantined") == "false"
        and controls.get("models_paused") == "false"
        and controls.get("notifications_paused") == "false",
        "Theo is quarantined or paused",
    )
    require(controls.get("background_paused") == "true", "Pause background autonomy for the test")
    heartbeat = observer.rows(
        "SELECT max(heartbeat_at) t FROM lifecycle_intervals WHERE owner_id=?", (observer.owner,)
    )[0]["t"]
    require(heartbeat and time.time() - heartbeat < 90, "Start the real Theo daemon first")
    tag = "theoe2e" + uuid.uuid4().hex[:12]
    mascot = "violet-" + uuid.uuid4().hex[:10]
    document_secret = "amber-" + uuid.uuid4().hex[:10]
    cases = [
        {
            "name": "model_roundtrip",
            "prompt": f"[{tag}-echo] Reply with exactly {tag}-OK.",
            "expected": tag + "-OK",
        },
        {
            "name": "durable_memory_tool",
            "prompt": f"[{tag}-save] Use remember to save this synthetic test fact: Project {tag} has mascot {mascot}. Confirm with {tag}-SAVED.",
            "expected": tag + "-SAVED",
            "tool": "remember",
        },
        {
            "name": "memory_recall_tool",
            "prompt": f"[{tag}-recall] Use recall to search project {tag}, then reply with its mascot.",
            "expected": mascot,
            "tool": "recall",
        },
        {
            "name": "document_to_model",
            "prompt": f"[{tag}-document] Read the attached document and reply with the value of document_secret.",
            "expected": document_secret,
            "document": "document_secret=" + document_secret + "\nThis is synthetic E2E data.\n",
        },
    ]
    report = {
        "scope": "live Telegram -> daemon -> native model/tools -> Telegram",
        "tag": tag,
        "backend": args.backend,
        "model": args.model,
        "cases": [],
    }
    try:
        await client.connect()
        require(await client.is_user_authorized(), "Run --login-only first")
        me = await client.get_me()
        require(
            not me.bot and me.id == settings.get("telegram_owner_id"),
            "Telegram user must match Theo's allowed owner",
        )
        require(
            me.id == settings.get("telegram_chat_id"), "Suite requires a private owner/bot chat"
        )
        bot = await client.get_entity(args.bot)
        require(bot.bot, "--bot must resolve to a bot")
        if args.transport_only:
            from telegram_transport_cases import run_transport

            return await run_transport(client, bot, observer, settings, args)
        if args.media_cases:
            media_cases = json.loads(args.media_cases.read_text())
            require(isinstance(media_cases, list), "Media cases must be a JSON list")
            for item in media_cases:
                require(
                    all(key in item for key in ("name", "path", "kind", "prompt", "expected")),
                    "Each media case needs name, path, kind, prompt and expected",
                )
                require(
                    item["kind"]
                    in (
                        "photo",
                        "document",
                        "audio",
                        "voice",
                        "video",
                        "animation",
                        "sticker",
                        "video_note",
                    ),
                    "Unsupported media fixture kind",
                )
                item["prompt"] = f"[{tag}-{item['name']}] " + item["prompt"]
                cases.append(item)
        for case in cases:
            started = time.monotonic()
            result = {"name": case["name"], "passed": False}
            try:
                if "path" in case:
                    source = (args.media_cases.parent / case["path"]).resolve()
                    require(source.is_file(), "Media fixture missing")
                    sent = await client.send_file(
                        bot,
                        str(source),
                        caption=case["prompt"],
                        voice_note=case["kind"] == "voice",
                        video_note=case["kind"] == "video_note",
                        force_document=case["kind"] == "document",
                        parse_mode=None,
                    )
                elif "document" in case:
                    document = io.BytesIO(case["document"].encode())
                    document.name = tag + ".txt"
                    sent = await client.send_file(
                        bot, document, caption=case["prompt"], force_document=True, parse_mode=None
                    )
                else:
                    sent = await client.send_message(bot, case["prompt"], parse_mode=None)
                trace = await wait_trace(observer, case["prompt"], args.timeout)
                texts = check_trace(
                    trace, args.backend, args.model, case["expected"], case.get("tool")
                )
                # IDs differ between Telegram user and bot views. Match actual text after
                # our own sent message, rather than comparing cross-account message IDs.
                end = time.monotonic() + 20
                received = []
                while time.monotonic() < end:
                    received = [
                        m.raw_text
                        for m in await client.get_messages(bot, min_id=sent.id, limit=100)
                        if not m.out and m.sender_id == bot.id
                    ]
                    if all(received.count(text) == texts.count(text) for text in set(texts)):
                        break
                    await asyncio.sleep(1)
                require(
                    all(received.count(text) == texts.count(text) for text in set(texts)),
                    "Telegram user did not receive exactly the receipted final chunks",
                )
                if case.get("tool") == "remember":
                    saved = observer.rows(
                        "SELECT r.body FROM memory_revisions r JOIN memory_records m "
                        "ON m.id=r.memory_id AND m.revision=r.version WHERE m.owner_id=? "
                        "AND m.status='active' AND r.source=?",
                        (observer.owner, "run:" + trace["runs"][0]["id"]),
                    )
                    require(
                        any(mascot in r["body"] for r in saved), "No durable memory from this run"
                    )
                if "document" in case:
                    parts = json.loads(trace["job"]["payload"])["parts"]
                    require(
                        any(
                            p.get("artifact_id") and document_secret in p.get("text", "")
                            for p in parts
                        ),
                        "Document was not hydrated into canonical model input",
                    )
                result.update(
                    passed=True,
                    job_id=trace["job"]["id"],
                    run_id=trace["runs"][0]["id"],
                    reply=texts,
                    tool_names=[t["tool"] for t in trace["tools"]],
                )
            except Exception as exc:
                # Avoid recording provider exceptions that might contain credentials.
                result["error"] = str(exc) if isinstance(exc, CheckFailed) else type(exc).__name__
            result["seconds"] = round(time.monotonic() - started, 3)
            report["cases"].append(result)
            print(json.dumps(result), flush=True)
            save_report(report, args.output)
            if not result["passed"]:
                break  # Do not pile more model work on a blocked pipeline.
        report["passed"] = len(report["cases"]) == len(cases) and all(
            c["passed"] for c in report["cases"]
        )
        report["not_run"] = [c["name"] for c in cases[len(report["cases"]) :]]
        save_report(report, args.output)
        return 0 if report["passed"] else 1
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login-only", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--transport-only",
        action="store_true",
        help="Exercise controls, edits, replies and configured topics without model inference",
    )
    parser.add_argument(
        "--media-cases",
        type=Path,
        help="JSON manifest of synthetic media files, prompts and expected answers",
    )
    parser.add_argument("--session", type=Path, default=Path.home() / ".local/share/theo-e2e/user")
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--bot", help="Your dedicated Theo bot's @username")
    parser.add_argument("--backend", choices=["codex", "claude", "cursor", "grok"])
    parser.add_argument("--model", help="Exact included model ID configured in Theo")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output", type=Path, default=Path("e2e-results"))
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except Exception as exc:
        print(
            "E2E setup failed: "
            + (str(exc) if isinstance(exc, CheckFailed) else type(exc).__name__)
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
