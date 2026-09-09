#!/usr/bin/env python3
"""Queue labelled, synthetic Telegram transport cases through the real delivery ledger.

This requires an already-running dedicated test daemon. It never polls Telegram,
calls a model, changes account evidence, or qualifies native model conversations.
"""

import argparse
import asyncio
import os
from pathlib import Path

from theo.config import load_settings
from theo.content.artifacts import Artifacts
from theo.delivery.ledger import Delivery
from theo.domain import Json
from theo.storage import Database

CASES = (
    "photo",
    "document",
    "voice",
    "audio",
    "video",
    "animation",
    "sticker",
    "video_note",
    "album",
    "location",
    "venue",
    "contact",
    "poll",
    "links",
    "long_text",
)


async def run(args: argparse.Namespace) -> None:
    root = args.data_root.expanduser().resolve()
    settings = load_settings(root)
    if settings.name != "Theo Test" or settings.telegram_chat_id != args.chat_id:
        raise ValueError("Use the dedicated Theo Test root and its exact paired private chat")
    db = Database(root)
    try:
        await db.initialize(settings.owner_id, settings.timezone)
        binding = await db.one(
            "SELECT conversation_id FROM telegram_destinations WHERE owner_id=? AND chat_id=? AND private=1",
            (settings.owner_id, args.chat_id),
        )
        if not binding:
            raise ValueError("Pair the test bot before running transport probes")
        key = "outbound-probe:" + args.tag + ":" + args.case
        existing = await db.one(
            "SELECT id,status FROM actions WHERE owner_id=? AND semantic_key=?",
            (settings.owner_id, key),
        )
        if existing:
            print({"case": args.case, "existing": existing})
            return
        store = Artifacts(db, settings)

        async def artifact(name: str) -> str:
            path = args.fixtures.resolve() / name
            value = await store.store(
                path.read_bytes(), name, "Synthetic Telegram transport fixture"
            )
            return str(value["id"])

        tag = f"[TG-OUT-{args.tag}-{args.case}]"
        files = {
            "photo": "synthetic-shapes.png",
            "document": "synthetic-note.txt",
            "voice": "synthetic-voice.ogg",
            "audio": "synthetic-audio.mp3",
            "video": "synthetic-video.mp4",
            "animation": "synthetic-animation.gif",
            "sticker": "synthetic-sticker.png",
            "video_note": "synthetic-video-note.mp4",
        }
        operation = "send_" + args.case
        request: Json
        if args.case in files:
            request = {"artifact_id": await artifact(files[args.case])}
            if args.case not in ("sticker", "video_note"):
                request["caption"] = tag + " Synthetic media; transport check only."
            else:
                await Delivery(db, settings).prepare(
                    binding["conversation_id"],
                    "send_message",
                    {"text": tag + " The next item is synthetic media; transport check only."},
                    key + ":label",
                    role="progress",
                    durable_obligation=True,
                )
        elif args.case == "album":
            operation = "send_media_group"
            request = {
                "items": [
                    {
                        "kind": "photo",
                        "artifact_id": await artifact(files["photo"]),
                        "caption": tag + " Synthetic album, image.",
                    },
                    {
                        "kind": "video",
                        "artifact_id": await artifact(files["video"]),
                        "caption": tag + " Synthetic album, video.",
                    },
                ]
            }
        elif args.case in ("location", "venue"):
            request = {"latitude": 0.0, "longitude": 0.0}
            if args.case == "venue":
                request.update(
                    title="Theo synthetic test venue",
                    address="Test coordinate: 0 degrees north, 0 degrees east",
                )
        elif args.case == "contact":
            request = {
                "phone_number": "+12025550129",
                "first_name": "Theo Synthetic",
                "last_name": "Test Contact",
            }
        elif args.case == "poll":
            request = {
                "question": tag + " Choose a synthetic test color",
                "options": ["Teal", "Amber"],
                "is_anonymous": False,
            }
        elif args.case == "links":
            operation = "send_message"
            request = {
                "text": tag
                + " Synthetic formatting check.\n\n**Bold** and *italic*.\n[Example link](https://example.com).\n> Quoted synthetic text.\n- First item\n- Second item\n```python\nprint('ORBIT-29')\n```\nLiteral <tag> & ampersand."
            }
        else:
            operation = "send_message"
            request = {
                "text": tag
                + " Synthetic long delivery check.\n"
                + "\n".join(
                    f"Line {n:03}: orbit 29 — synthetic message chunk continuity."
                    for n in range(1, 181)
                )
                + "\nEND-ORBIT29"
            }
        if args.reply_to:
            request["reply_to"] = args.reply_to
        action = await Delivery(db, settings).prepare(
            binding["conversation_id"],
            operation,
            request,
            key,
            role="progress",
            durable_obligation=True,
        )
        print({"case": args.case, "action_id": action, "model_called": False})
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--reply-to", type=int)
    args = parser.parse_args()
    if not args.live or any(
        os.environ.get(key) for key in ("CI", "GITHUB_ACTIONS", "THEO_TEST_OFFLINE")
    ):
        parser.error("This local-only probe requires --live and cannot run in offline tests or CI")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
