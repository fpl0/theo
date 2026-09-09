"""Independent daemon supervision with bounded restart and recovery alerts.

Writes host heartbeats, honors maintenance pauses and generates macOS service
definitions. Core state mutation and native inference remain in the daemon.
"""

import argparse
import asyncio
import contextlib
import json
import os
import plistlib
import signal
import sys
import time
from pathlib import Path

from theo.execution.processes import terminate_tree


async def alert_owner(root: Path, circuit_open: bool) -> None:
    """Narrow independent health notification; no database or model tool authority."""
    if os.environ.get("THEO_TEST_OFFLINE") == "1":
        return
    import aiohttp

    from theo.config import load_settings

    settings = load_settings(root)
    if settings.telegram_chat_id is None:
        return
    token = os.environ.get("THEO_HEALTH_TOKEN")
    if token is None and sys.platform == "darwin":
        process = await asyncio.create_subprocess_exec(
            "security",
            "find-generic-password",
            "-s",
            "theo.health",
            "-w",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        raw, _ = await asyncio.wait_for(process.communicate(), 10)
        if process.returncode == 0:
            token = raw.decode().strip()
    if not token:
        return
    text = "Theo's core is unavailable. " + (
        "The restart circuit is open; operator attention is needed."
        if circuit_open
        else "The supervisor is attempting recovery."
    )
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": settings.telegram_chat_id, "text": text},
            ) as response:
                result = "accepted" if response.status == 200 else "rejected"
    except Exception:
        result = "uncertain"
    (root / "health-alert-receipt.json").write_text(
        json.dumps({"timestamp": time.time(), "status": result})
    )


def service_definition(root: Path, executable: Path) -> bytes:
    return plistlib.dumps(
        {
            "Label": "local.theo.supervisor",
            "ProgramArguments": [
                str(executable),
                "-m",
                "theo.supervisor",
                "--data-root",
                str(root.resolve()),
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "ProcessType": "Background",
            "WorkingDirectory": str(root.resolve()),
            "StandardOutPath": str(root / "supervisor.stdout.log"),
            "StandardErrorPath": str(root / "supervisor.stderr.log"),
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        }
    )


async def supervise(root: Path) -> None:
    stopped = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, stopped.set)
    child: asyncio.subprocess.Process | None = None
    failures: list[float] = []
    restart_after = 0.0
    started = 0.0
    try:
        while not stopped.is_set():
            timestamp = time.time()
            (root / "supervisor-heartbeat.json").write_text(
                json.dumps(
                    {
                        "timestamp": timestamp,
                        "pid": os.getpid(),
                        "core_pid": child.pid if child else None,
                    }
                )
            )
            maintenance = (root / "maintenance.pause").exists()
            failures = [failure for failure in failures if timestamp - failure < 3600]
            if maintenance and child:
                await asyncio.to_thread(terminate_tree, child.pid)
                await child.wait()
                child = None
            if (
                not maintenance
                and child is None
                and timestamp >= restart_after
                and len(failures) < 5
            ):
                release = root / "releases/current/bin/python"
                executable = str(release) if release.exists() else sys.executable
                child = await asyncio.create_subprocess_exec(
                    executable,
                    "-m",
                    "theo",
                    "--data-root",
                    str(root),
                    "serve",
                    start_new_session=True,
                )
                started = timestamp
            if child:
                stale = False
                try:
                    heartbeat = json.loads((root / "heartbeat.json").read_text())
                    stale = timestamp - started > 90 and (
                        heartbeat.get("pid") != child.pid or timestamp - heartbeat["timestamp"] > 90
                    )
                except OSError, ValueError, KeyError:
                    stale = timestamp - started > 90
                if child.returncode is not None or stale:
                    await asyncio.to_thread(terminate_tree, child.pid)
                    await child.wait()
                    failures.append(timestamp)
                    if len(failures) in (1, 5):
                        with contextlib.suppress(Exception):
                            await alert_owner(root, len(failures) >= 5)
                    restart_after = timestamp + min(300, 5 * 2 ** len(failures))
                    (root / "supervisor-alert.json").write_text(
                        json.dumps(
                            {
                                "timestamp": timestamp,
                                "reason": "core_unhealthy",
                                "failures_last_hour": len(failures),
                                "circuit_open": len(failures) >= 5,
                            }
                        )
                    )
                    child = None
            try:
                await asyncio.wait_for(stopped.wait(), 5)
            except TimeoutError:
                pass
    finally:
        if child:
            await asyncio.to_thread(terminate_tree, child.pid)
            await child.wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(supervise(args.data_root))


if __name__ == "__main__":
    main()
