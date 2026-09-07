import asyncio
import json
import os
import signal
import socket
import sys

import pytest

from theo.config import Settings, save_settings
from theo.storage import Database


async def test_a33_core_crash_recovers_and_maintenance_does_not_restart(tmp_path):
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.close()
    except PermissionError:
        pytest.skip(
            "Host denies Unix server sockets; supervisor/core integration requires a service-capable host"
        )
    root = tmp_path / "supervised"
    save_settings(root, Settings())
    db = Database(root)
    await db.initialize()
    await db.close()
    env = {key: value for key, value in os.environ.items() if key != "THEO_TELEGRAM_TOKEN"}
    supervisor = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "theo.supervisor",
        "--data-root",
        str(root),
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    async def until(check, seconds=35):
        async with asyncio.timeout(seconds):
            while True:
                try:
                    value = check()
                    if value:
                        return value
                except OSError, ValueError, KeyError:
                    pass
                await asyncio.sleep(0.1)

    def heartbeat():
        return json.loads((root / "heartbeat.json").read_text())

    try:
        first = await until(heartbeat)
        os.kill(first["pid"], signal.SIGKILL)
        await until(lambda: (root / "supervisor-alert.json").exists())
        second = await until(lambda: heartbeat() if heartbeat()["pid"] != first["pid"] else None)
        assert second["pid"] != first["pid"]
        (root / "maintenance.pause").touch()
        await until(
            lambda: (
                json.loads((root / "supervisor-heartbeat.json").read_text()).get("core_pid") is None
            )
        )
        alert = json.loads((root / "supervisor-alert.json").read_text())
        assert alert["failures_last_hour"] == 1
        assert supervisor.returncode is None
    finally:
        supervisor.terminate()
        await asyncio.wait_for(supervisor.wait(), 10)
