"""Download compatible, hash-bound PyPI wheels without executing candidate hooks.

Only the fixed PyPI artifact host is reachable through this downloader. All
installation and source distribution execution remains inside the builder sandbox.
"""

import hashlib
import os
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename

from theo.domain import Denied, Json


def wheel_requests(lock: bytes) -> list[Json]:
    packages = tomllib.loads(lock.decode()).get("package", [])
    if len(packages) > 500:
        raise Denied("Dependency lock exceeds the package limit")
    tags = set(sys_tags())
    result: list[Json] = []
    for package in packages:
        source = package.get("source", {})
        if source in ({"editable": "."}, {"virtual": "."}):
            continue
        if source != {"registry": "https://pypi.org/simple"}:
            raise Denied("Maintenance dependency staging supports only hash-locked PyPI wheels")
        for wheel in package.get("wheels", []):
            url = urlsplit(str(wheel["url"]))
            name = Path(url.path).name
            if (
                url.scheme != "https"
                or url.hostname != "files.pythonhosted.org"
                or url.port not in (None, 443)
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise Denied("Dependency artifact is outside the approved PyPI host")
            _, _, _, wheel_tags = parse_wheel_filename(name)
            if not tags.intersection(wheel_tags):
                continue
            checksum = wheel.get("hash", "")
            size = wheel.get("size", 0)
            if (
                not checksum.startswith("sha256:")
                or len(checksum) != 71
                or not 0 < size <= 200 * 1024 * 1024
            ):
                raise Denied("Dependency wheel lacks a bounded SHA-256 identity")
            result.append({"name": name, "url": wheel["url"], "sha256": checksum[7:], "size": size})
            break
    if sum(item["size"] for item in result) > 2_000_000_000:
        raise Denied("Dependency download budget exceeded")
    return result


async def stage(lock: Path, destination: Path) -> Json:
    requests = wheel_requests(lock.read_bytes())
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False) as client:
        for artifact in requests:
            target = destination / artifact["name"]
            if (
                target.is_file()
                and hashlib.sha256(target.read_bytes()).hexdigest() == artifact["sha256"]
            ):
                continue
            temporary = target.with_suffix(".partial")
            hasher = hashlib.sha256()
            size = 0
            try:
                async with client.stream("GET", artifact["url"]) as response:
                    if response.status_code != 200:
                        raise Denied("Dependency artifact download was not accepted")
                    with temporary.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > artifact["size"]:
                                raise Denied("Dependency artifact exceeds its locked size")
                            hasher.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                if size != artifact["size"] or hasher.hexdigest() != artifact["sha256"]:
                    raise Denied("Dependency artifact differs from the lock")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
    return {"lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(), "wheels": len(requests)}
