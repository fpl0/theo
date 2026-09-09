#!/usr/bin/env python3
"""Inject acknowledgement loss after one real, labelled test-bot send.

Stop the dedicated test daemon first. This process holds its lock and never
polls or calls a model. Resume the daemon and confirm delivery in Telegram.
"""

import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path

from telegram_setup import read_private_token

from theo.channels.telegram.adapter import Telegram
from theo.config import load_settings
from theo.delivery.ledger import Delivery
from theo.domain import Json
from theo.storage import Database


async def run(args: argparse.Namespace) -> None:
    root = args.data_root.expanduser().resolve()
    settings = load_settings(root)
    if settings.name != "Theo Test" or settings.telegram_chat_id != args.chat_id:
        raise ValueError("Use the dedicated Theo Test root and its exact private chat")
    with (root / "daemon.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = Database(root)
        telegram = Telegram(db, settings, read_private_token(args.token_file))
        try:
            await db.initialize(settings.owner_id, settings.timezone)
            me = await telegram.bot.get_me(request_timeout=10)
            if me.username != args.bot.lstrip("@"):
                raise ValueError("Token belongs to a different bot")
            binding = await db.one(
                "SELECT conversation_id FROM telegram_destinations WHERE owner_id=? AND bot_id=? AND chat_id=? AND private=1",
                (settings.owner_id, me.id, args.chat_id),
            )
            if not binding:
                raise ValueError("Pair this exact bot and private chat before the probe")
            key = "uncertainty-probe:" + args.tag
            existing = await db.one(
                "SELECT id,status FROM actions WHERE semantic_key=? AND owner_id=?",
                (key, settings.owner_id),
            )
            if existing:
                print(json.dumps({"existing": existing, "resent": False}))
                return
            if await db.one("SELECT id FROM outbox WHERE status IN ('ready','executing') LIMIT 1"):
                raise ValueError("Drain existing deliveries before running the isolated probe")
            delivery = Delivery(db, settings)
            text = f"[TG-UNCERTAINTY-{args.tag}] Synthetic delivery test. This message arrived, but its acknowledgement will intentionally be withheld from Theo's delivery ledger. Confirm it in Telegram; it must not be resent."
            action = await delivery.prepare(
                binding["conversation_id"],
                "send_message",
                {"text": text},
                key,
                role="progress",
                durable_obligation=True,
            )
            received: Json = {}

            async def lose_acknowledgement(operation: str, payload: Json) -> Json:
                payload.pop("_channel", None)
                received.update(await telegram.send(operation, payload))
                raise ConnectionResetError("Injected acknowledgement loss after remote acceptance")

            await delivery.dispatch_one(lose_acknowledgement)
            assert received.get("message_id")
            assert not await delivery.dispatch_one(lose_acknowledgement)
            assert not await delivery.dispatch_one(lose_acknowledgement)
            chunk = await db.one(
                "SELECT id,status,attempts FROM outbox WHERE action_id=?", (action,)
            )
            assert chunk and chunk["status"] == "uncertain" and chunk["attempts"] == 1
            print(
                json.dumps(
                    {
                        "action_id": action,
                        "chunk": chunk,
                        "actual_received_message_id": received["message_id"],
                        "injected_fault": "Host discarded the successful send result before ledger receipt recording",
                        "model_called": False,
                        "automatic_retry": False,
                        "reply_command": f"/delivered {action} {chunk['id']}",
                    }
                )
            )
        finally:
            await telegram.close()
            await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--bot", required=True)
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    if not args.live or any(
        os.environ.get(key) for key in ("CI", "GITHUB_ACTIONS", "THEO_TEST_OFFLINE")
    ):
        parser.error("This test-only fault probe requires --live and refuses offline/CI execution")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
