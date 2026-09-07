"""Public web reads with DNS-pinned connections and guarded redirects."""

import asyncio
import ipaddress
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from theo.domain import Denied, Json


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 80, 443)
    ):
        raise Denied("Only public HTTP(S) URLs on standard ports are allowed")
    host = parsed.hostname.rstrip(".").lower()
    if host in ("localhost", "metadata.google.internal") or host.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise Denied("Private hosts are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise Denied("Private addresses are not allowed")


class PublicResolver(AbstractResolver):
    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[ResolveResult]:
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, family=family
        )
        result: list[ResolveResult] = []
        for address_family, _, protocol, _, sockaddr in records:
            address = str(sockaddr[0])
            if not ipaddress.ip_address(address).is_global:
                raise Denied("DNS resolved to a private address")
            result.append(
                ResolveResult(
                    hostname=host,
                    host=address,
                    port=port,
                    family=address_family,
                    proto=protocol,
                    flags=socket.AI_NUMERICHOST,
                )
            )
        return result

    async def close(self) -> None:
        return None


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript"):
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


async def fetch(url: str, max_bytes: int = 4 * 1024 * 1024) -> tuple[str, str, bytes]:
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(
        connector=connector, trust_env=False, timeout=aiohttp.ClientTimeout(total=30)
    ) as client:
        for _ in range(6):
            validate_url(url)
            async with client.get(
                url, allow_redirects=False, headers={"User-Agent": "Theo/0.1 public-research"}
            ) as response:
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers["Location"])
                    continue
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError("Web response exceeds download limit")
                return (
                    url,
                    response.headers.get("Content-Type", "application/octet-stream"),
                    bytes(body),
                )
    raise Denied("Redirect limit exceeded")


async def browse(url: str) -> Json:
    final_url, mime, raw = await fetch(url)
    if "html" in mime:
        parser = TextExtractor()
        parser.feed(raw.decode("utf-8", errors="replace"))
        text = "\n".join(parser.parts)
    elif mime.startswith("text/") or "json" in mime:
        text = raw.decode("utf-8", errors="replace")
    else:
        raise ValueError("Use artifact import for non-text web resources")
    return {
        "url": final_url,
        "text": text[:80000],
        "truncated": len(text) > 80000,
        "untrusted": True,
        "mime": mime,
    }


async def render_public_page(url: str) -> bytes:
    from playwright.async_api import async_playwright

    validate_url(url)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            context = await browser.new_context(service_workers="block", accept_downloads=False)

            async def intercept(route: Any) -> None:
                try:
                    if route.request.method != "GET":
                        await route.abort()
                        return
                    _, mime, raw = await fetch(route.request.url, 2 * 1024 * 1024)
                    await route.fulfill(status=200, content_type=mime, body=raw)
                except Exception:
                    await route.abort()

            await context.route("**/*", intercept)
            await context.route_web_socket("**/*", lambda route: route.close())
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return await page.screenshot(full_page=False)
        finally:
            await browser.close()
