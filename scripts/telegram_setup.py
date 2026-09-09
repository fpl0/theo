#!/usr/bin/env python3
"""Pair a dedicated test bot and run Theo; optional owner-only token file."""

import argparse
import asyncio
import getpass
import html
import os
import secrets
import socket
import stat
from pathlib import Path

from aiogram import Bot
from aiogram.utils.token import validate_token
from aiohttp import web

from theo.application.service import serve
from theo.channels.telegram.state import TelegramState
from theo.config import Settings, load_settings, save_settings
from theo.storage import Database


def read_private_token(path: Path) -> str:
    fd = os.open(path.expanduser(), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("Token file must be owner-owned with no group/other access")
        return stream.read(201).strip()


def save_private_token(path: Path, token: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if not secrets.compare_digest(read_private_token(path), token):
            raise ValueError("Refusing to replace a different stored token")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(token + "\n")
        stream.flush()
        os.fsync(stream.fileno())


async def browser_token(*, save_path: Path | None = None) -> str:
    """Accept one token on a nonce-protected loopback form, without access logs."""
    token_result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    route = "/" + secrets.token_urlsafe(32)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"

    async def form(request: web.Request) -> web.Response:
        if request.host != origin.removeprefix("http://"):
            raise web.HTTPForbidden()
        headers = {
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
        }
        if request.method == "GET":
            notice = (
                "The token stays in this local process and is never saved."
                if save_path is None
                else "The token will be saved with owner-only access at " + str(save_path) + "."
            )
            return web.Response(
                text="<title>Connect Theo Test</title><h1>Connect Theo Test</h1>"
                f"<p>{html.escape(notice)}</p>"
                f'<form method="post" action="{route}"><label>Test bot token '
                '<input name="token" type="password" autocomplete="off" required '
                'maxlength="200"></label><button type="submit">Connect test bot</button></form>'
                f'<p id="status"></p><script src="{route}/form.js"></script>',
                content_type="text/html",
                headers=headers,
            )
        if request.headers.get("Origin") != origin or token_result.done():
            raise web.HTTPForbidden()
        fields = await request.post()
        token = str(fields.get("token", "")).strip()
        try:
            validate_token(token)
        except Exception:
            raise web.HTTPBadRequest(text="Invalid bot token") from None
        token_result.set_result(token)
        return web.Response(text="Token received. Continue pairing in Telegram.", headers=headers)

    app = web.Application(client_max_size=4096)

    async def script(request: web.Request) -> web.Response:
        return web.Response(
            text="document.querySelector('form').addEventListener('submit', async event => {"
            "event.preventDefault(); const form = event.target;"
            "const status = document.querySelector('#status');"
            "status.textContent = 'Connecting...';"
            "try { const result = await fetch(form.action, {method: 'POST',"
            "body: new URLSearchParams(new FormData(form))});"
            "status.textContent = await result.text();"
            "if (result.ok) form.remove();"
            "} catch { status.textContent = 'Connection ended; check the local service.'; }"
            "});",
            content_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    app.router.add_get(route + "/form.js", script)
    app.router.add_get(route, form)
    app.router.add_post(route, form)
    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        await web.SockSite(runner, listener).start()
        print(f"One-time local token form: {origin}{route}", flush=True)
        return await asyncio.wait_for(token_result, 600)
    finally:
        await runner.cleanup()
        listener.close()


async def run(args: argparse.Namespace, token: str) -> None:
    bot = Bot(token=token)
    root = args.data_root.expanduser().resolve()
    try:
        me = await bot.get_me(request_timeout=15)
        if me.username != args.bot.lstrip("@"):
            raise ValueError("The supplied token belongs to a different bot")
        webhook = await bot.get_webhook_info(request_timeout=15)
        if webhook.url:
            raise ValueError("This bot has an active webhook; use a dedicated unused test bot")
        if (root / "config.json").exists():
            settings = load_settings(root)
            if settings.name != "Theo Test":
                raise ValueError("Refusing to reuse a non-test data root")
            print("Resuming the configured test root.", flush=True)
        else:
            nonce = secrets.token_hex(8)
            print(f"Open https://t.me/{me.username} and send: /start pair-{nonce}", flush=True)
            print("Waiting for the private-chat pairing message…", flush=True)
            paired = None
            while paired is None:
                updates = await bot.get_updates(timeout=25, allowed_updates=["message"])
                for update in updates:
                    message = update.message
                    if (
                        message
                        and message.from_user
                        and message.chat.type == "private"
                        and message.from_user.id == message.chat.id
                        and message.text == f"/start pair-{nonce}"
                    ):
                        paired = update
                        break
                if paired is None:
                    await asyncio.sleep(1)
            assert paired.message and paired.message.from_user
            settings = Settings(
                name="Theo Test",
                telegram_owner_id=paired.message.from_user.id,
                telegram_chat_id=paired.message.chat.id,
                telegram_keychain_service="theo.telegram.test",
            )
            save_settings(root, settings)
            db = Database(root)
            try:
                await db.initialize(settings.owner_id, settings.timezone)
                state = TelegramState(db, settings, me.id)
                await state.destination(settings.telegram_chat_id)
                await state.receive(paired)
            finally:
                await db.close()
            print(f"Paired owner/chat {settings.telegram_chat_id}. Data root: {root}", flush=True)
        if args.save_token:
            save_private_token(args.save_token, token)
            print(f"Token saved with owner-only access: {args.save_token}", flush=True)
        if args.pair_only:
            print("Test bot setup complete; no daemon started.", flush=True)
            return
        print(
            "Starting Theo Test. Native model execution requires account/isolation setup. Keep this terminal open. Ctrl-C stops the bot.",
            flush=True,
        )
    finally:
        await bot.session.close()
    db = Database(root)
    try:
        await db.initialize(settings.owner_id, settings.timezone)
        if args.presentation_check:
            from telegram_presentation_probe import presentation_probe

            await presentation_probe(db, settings, token)
        await serve(db, settings, token)
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot", required=True)
    parser.add_argument("--token-browser", action="store_true", help="Use a one-time local form")
    parser.add_argument("--token-file", type=Path, help="Read an existing owner-only token file")
    parser.add_argument("--save-token", type=Path, help="Save the verified token with mode 0600")
    parser.add_argument(
        "--pair-only", action="store_true", help="Finish setup without starting a daemon"
    )
    parser.add_argument(
        "--presentation-check",
        action="store_true",
        help="Send a labelled synthetic typing/draft probe",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path.home() / "Library/Application Support/Theo-Telegram-Test",
    )
    args = parser.parse_args()

    async def start() -> None:
        token = (
            read_private_token(args.token_file)
            if args.token_file
            else os.environ.get("THEO_TELEGRAM_TOKEN")
        )
        if not token:
            token = (
                await browser_token(save_path=args.save_token)
                if args.token_browser
                else getpass.getpass("Test bot token (hidden): ")
            )
        await run(args, token.strip())

    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print("Setup stopped: " + type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
