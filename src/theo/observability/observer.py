"""Independent read-only host observer and local alert receipt endpoint.

Collects daemon, SQLite, container and memory health for Prometheus, records
alert receipts and checks the configured external heartbeat without core authority.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import httpx
import psutil
from aiohttp import web
from prometheus_client import CollectorRegistry, Gauge, generate_latest


class Observer:
    def __init__(self, root: Path):
        self.root = root
        self.registry = CollectorRegistry()
        self.gauges: dict[str, Gauge] = {}
        self.last_refresh = 0.0

    def set(self, name: str, value: float, **labels: str) -> None:
        labels = {"environment": os.getenv("THEO_ENVIRONMENT", "local"), **labels}
        if name not in self.gauges:
            self.gauges[name] = Gauge(
                name, name.replace("_", " "), list(labels), registry=self.registry
            )
        gauge = self.gauges[name]
        if labels:
            gauge = gauge.labels(**labels)
        gauge.set(value)

    def snapshot(self) -> None:
        now = time.time()
        self.set("theo_observer_up", 1)
        self.set(
            "theo_observability_test_alert", float((self.root / "telemetry-test-alert").exists())
        )
        self.set("theo_observer_refresh_timestamp_seconds", now)
        self.set("theo_host_memory_used_bytes", psutil.virtual_memory().used)
        self.set("theo_host_memory_total_bytes", psutil.virtual_memory().total)
        self.set("theo_host_cpu_ratio", psutil.cpu_percent() / 100)
        self.set("theo_host_disk_free_bytes", psutil.disk_usage(str(self.root)).free)
        self.set("theo_host_disk_used_ratio", psutil.disk_usage(str(self.root)).percent / 100)
        self.set("theo_observer_memory_bytes", psutil.Process().memory_info().rss)
        report_path = os.getenv("THEO_OBSERVABILITY_QUALIFICATION", "")
        qualified = False
        if report_path and os.getenv("THEO_DOCKER_CONTEXT") == "colima-theo-observability":
            with contextlib.suppress(OSError, ValueError, TypeError):
                report = json.loads(Path(report_path).read_text())
                qualified = (
                    report.get("passed") is True and report.get("budget_bytes") == 2_000_000_000
                )
        self.set("theo_observability_budget_verified", float(qualified))
        self.set("theo_observability_budget_bytes", 2_000_000_000)
        for name, gauge in self.gauges.items():
            if name in {
                "theo_jobs_current",
                "theo_outbox_current",
                "theo_controls",
                "theo_telegram_events_current",
                "theo_accounts_current",
                "theo_goals_current",
                "theo_codex_runtime_info",
                "theo_codex_models_current",
            }:
                gauge.clear()
        path = self.root / "theo.sqlite3"
        try:
            with contextlib.closing(
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
            ) as db:
                db.execute("PRAGMA query_only=ON")
                heartbeat = db.execute(
                    "SELECT max(heartbeat_at) FROM lifecycle_intervals WHERE ended_at IS NULL"
                ).fetchone()[0]
                self.set("theo_core_heartbeat_timestamp_seconds", heartbeat or 0)
                self.set("theo_core_ready", float(bool(heartbeat and now - heartbeat < 90)))
                self.set("theo_database_readable", 1)
                for status, count in db.execute(
                    "SELECT status,count(*) FROM goals GROUP BY status"
                ):
                    self.set("theo_goals_current", count, status=status)
                count, oldest = db.execute(
                    "SELECT count(*),min(next_due) FROM schedules WHERE active=1"
                ).fetchone()
                self.set("theo_schedules_active", count)
                self.set("theo_schedule_overdue_seconds", max(0, now - oldest) if oldest else 0)
                for model, status, count in db.execute(
                    "SELECT model,status,count(*) FROM runs WHERE backend='codex' AND started_at>? GROUP BY model,status",
                    (now - 86400,),
                ):
                    self.set(
                        "theo_codex_models_current", count, model=str(model)[:120], status=status
                    )
                runtime = db.execute(
                    "SELECT runtime_version,billing_mode FROM backend_accounts WHERE backend='codex' ORDER BY verified_at DESC LIMIT 1"
                ).fetchone()
                if runtime:
                    self.set(
                        "theo_codex_runtime_info",
                        1,
                        version=str(runtime[0])[:120],
                        billing_mode=runtime[1],
                    )
                for status, lane, channel, count in db.execute(
                    "SELECT j.status,j.lane,c.channel,count(*) FROM jobs j JOIN conversations c ON c.id=j.conversation_id GROUP BY 1,2,3"
                ):
                    self.set(
                        "theo_jobs_current",
                        count,
                        status=status,
                        lane=lane,
                        channel="cli" if channel == "local" else channel,
                    )
                oldest = db.execute(
                    "SELECT min(created_at) FROM jobs WHERE status='queued' AND available_at<=?",
                    (now,),
                ).fetchone()[0]
                self.set("theo_queue_oldest_seconds", max(0, now - oldest) if oldest else 0)
                for status, count in db.execute(
                    "SELECT status,count(*) FROM outbox GROUP BY status"
                ):
                    self.set("theo_outbox_current", count, status=status)
                oldest = db.execute(
                    "SELECT min(available_at) FROM outbox WHERE status='ready' AND available_at<=?",
                    (now,),
                ).fetchone()[0]
                self.set("theo_outbox_oldest_seconds", max(0, now - oldest) if oldest else 0)
                self.set(
                    "theo_approvals_pending",
                    db.execute(
                        "SELECT count(*) FROM approvals WHERE decision='pending'"
                    ).fetchone()[0],
                )
                self.set(
                    "theo_actions_uncertain",
                    db.execute("SELECT count(*) FROM actions WHERE status='uncertain'").fetchone()[
                        0
                    ],
                )
                latest = db.execute(
                    "SELECT max(ended_at) FROM runs WHERE status='completed'"
                ).fetchone()[0]
                self.set("theo_last_completion_timestamp_seconds", latest or 0)
                for key, value in db.execute("SELECT key,value FROM control"):
                    if key in {
                        "background_paused",
                        "notifications_paused",
                        "models_paused",
                        "quarantined",
                    }:
                        self.set("theo_controls", float(value == "true"), control=key)
                for status, count in db.execute(
                    "SELECT status,count(*) FROM telegram_events GROUP BY status"
                ):
                    self.set("theo_telegram_events_current", count, status=status)
                oldest = db.execute(
                    "SELECT min(received_at) FROM telegram_events WHERE status='pending'"
                ).fetchone()[0]
                self.set("theo_telegram_lag_seconds", max(0, now - oldest) if oldest else 0)
                for backend, status, count in db.execute(
                    "SELECT backend,status,count(*) FROM backend_accounts GROUP BY 1,2"
                ):
                    self.set("theo_accounts_current", count, backend=backend, status=status)
                for backend in ("codex", "claude", "cursor", "grok"):
                    latest = db.execute(
                        "SELECT max(verified_at) FROM backend_accounts WHERE backend=?", (backend,)
                    ).fetchone()[0]
                    self.set(
                        "theo_account_verified_timestamp_seconds", latest or 0, backend=backend
                    )
                self.set(
                    "theo_codex_usage_reported",
                    float(
                        db.execute(
                            "SELECT count(*) FROM usage_observations u JOIN runs r ON r.id=u.run_id WHERE r.backend='codex' AND (u.input_tokens IS NOT NULL OR u.output_tokens IS NOT NULL)"
                        ).fetchone()[0]
                        > 0
                    ),
                )
        except sqlite3.Error:
            self.set("theo_database_readable", 0)
            self.set("theo_core_ready", 0)
        try:
            config = json.loads((self.root / "config.json").read_text())
            self.set("theo_channel_configured", 1, channel="cli")
            self.set(
                "theo_channel_configured",
                float(config.get("telegram_chat_id") is not None),
                channel="telegram",
            )
        except OSError, ValueError:
            pass
        try:
            heartbeat_data = json.loads((self.root / "heartbeat.json").read_text())
            process = psutil.Process(heartbeat_data["pid"])
            self.set("theo_core_memory_bytes", process.memory_info().rss)
            self.set("theo_core_uptime_seconds", now - process.create_time())
        except OSError, ValueError, KeyError, psutil.Error:
            self.set("theo_core_memory_bytes", float("nan"))
            self.set("theo_core_uptime_seconds", float("nan"))
        self.last_refresh = now

    async def containers(self) -> None:
        process: asyncio.subprocess.Process | None = None
        docker = ["docker"]
        if os.getenv("THEO_DOCKER_CONTEXT"):
            docker += ["--context", os.environ["THEO_DOCKER_CONTEXT"]]
        try:
            process = await asyncio.create_subprocess_exec(
                *docker,
                "ps",
                "--filter",
                "label=com.docker.compose.project=theo-observability",
                "--format",
                "{{.Names}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            names, _ = await asyncio.wait_for(process.communicate(), 5)
            if not names.strip():
                self.set("theo_container_stats_available", 0)
                return
            process = await asyncio.create_subprocess_exec(
                *docker,
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
                *names.decode().split(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output, _ = await asyncio.wait_for(process.communicate(), 8)
            self.set("theo_container_stats_available", float(process.returncode == 0))
            for line in output.decode().splitlines():
                item = json.loads(line)
                service = item["Name"].removeprefix("theo-observability-").rsplit("-", 1)[0]
                if service not in {"grafana", "alloy", "prometheus", "loki", "tempo"}:
                    continue
                used, limit = item["MemUsage"].split(" / ")
                self.set("theo_container_memory_bytes", memory_bytes(used), component=service)
                self.set(
                    "theo_container_memory_limit_bytes", memory_bytes(limit), component=service
                )
                self.set(
                    "theo_container_cpu_ratio",
                    float(item["CPUPerc"].rstrip("%")) / 100,
                    component=service,
                )
            process = await asyncio.create_subprocess_exec(
                *docker,
                "inspect",
                "--format",
                '{"name":"{{.Name}}","restarts":{{.RestartCount}},"oom":{{.State.OOMKilled}}}',
                *names.decode().split(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output, _ = await asyncio.wait_for(process.communicate(), 5)
            for line in output.decode().splitlines():
                item = json.loads(line)
                service = item["name"].removeprefix("/theo-observability-").rsplit("-", 1)[0]
                self.set("theo_container_restarts", item["restarts"], component=service)
                self.set("theo_container_oom_killed", float(item["oom"]), component=service)
        except OSError, TimeoutError, ValueError, KeyError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            self.set("theo_container_stats_available", 0)

    async def memory(self) -> None:
        if os.getenv("THEO_DOCKER_CONTEXT") != "colima-theo-observability":
            return
        pids = {os.getpid()}
        for candidate in psutil.process_iter(["pid", "name", "cmdline"]):
            command = " ".join(candidate.info["cmdline"] or [])
            if candidate.info["name"] == "com.apple.Virtualization.VirtualMachine" or (
                candidate.info["name"] in {"limactl", "ssh"} and "/.colima/" in command
            ):
                pids.add(candidate.pid)
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/footprint",
                "--noCategories",
                "--swapped",
                "-f",
                "bytes",
                *[arg for pid in sorted(pids) for arg in ("-p", str(pid))],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output, _ = await asyncio.wait_for(process.communicate(), 10)
            match = re.search(rb"Summary Footprint: (\d+) B", output)
            if not match:
                self.set("theo_observability_memory_measurement_available", 0)
                return
            self.set("theo_observability_memory_bytes", int(match[1]))
            self.set("theo_observability_memory_measurement_available", 1)
            self.set("theo_host_swap_bytes", psutil.swap_memory().used)
        except OSError, TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            self.set("theo_observability_memory_measurement_available", 0)

    async def heartbeat(self) -> None:
        """External absence detection; send only while the core and all backends are healthy."""
        endpoint = os.getenv("THEO_HEARTBEAT_URL", "")
        self.set("theo_external_heartbeat_configured", float(bool(endpoint)))
        if not endpoint:
            return
        environment = {"environment": os.getenv("THEO_ENVIRONMENT", "local")}
        if self.registry.get_sample_value("theo_core_ready", environment) != 1:
            self.set("theo_external_heartbeat_healthy", 0)
            return
        urls = [
            "http://127.0.0.1:13000/api/health",
            "http://127.0.0.1:12345/-/ready",
            "http://127.0.0.1:19090/-/ready",
            "http://127.0.0.1:13100/ready",
            "http://127.0.0.1:13200/ready",
        ]
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                responses = await asyncio.gather(*(client.get(url) for url in urls))
                if not all(response.status_code == 200 for response in responses):
                    self.set("theo_external_heartbeat_healthy", 0)
                    return
                response = await client.get(endpoint, timeout=5)
                response.raise_for_status()
                self.set("theo_external_heartbeat_healthy", 1)
                self.set("theo_external_heartbeat_sent_timestamp_seconds", time.time())
        except httpx.HTTPError:
            # Never log the secret-bearing heartbeat URL.
            self.set("theo_external_heartbeat_healthy", 0)


def memory_bytes(text: str) -> float:
    for suffix, factor in (
        ("GiB", 1024**3),
        ("MiB", 1024**2),
        ("KiB", 1024),
        ("GB", 10**9),
        ("MB", 10**6),
        ("kB", 1000),
        ("B", 1),
    ):
        if text.endswith(suffix):
            return float(text.removesuffix(suffix)) * factor
    raise ValueError("Unknown memory unit")


async def application(root: Path) -> web.Application:
    observer = Observer(root)
    app = web.Application(client_max_size=128 * 1024)
    directory = root / "telemetry"
    directory.mkdir(exist_ok=True, mode=0o700)
    receipts = RotatingFileHandler(
        directory / "alert-receipts.jsonl", maxBytes=1_000_000, backupCount=2
    )

    async def metrics(request: web.Request) -> web.Response:
        return web.Response(body=generate_latest(observer.registry), content_type="text/plain")

    async def health(request: web.Request) -> web.Response:
        healthy = time.time() - observer.last_refresh < 60
        return web.json_response({"observer": healthy}, status=200 if healthy else 503)

    async def alerts(request: web.Request) -> web.Response:
        payload: dict[str, Any] = await request.json()
        receipt = {
            "received_at": time.time(),
            "status": payload.get("status"),
            "alerts": [
                {
                    "status": a.get("status"),
                    "alertname": a.get("labels", {}).get("alertname"),
                    "fingerprint": a.get("fingerprint"),
                }
                for a in payload.get("alerts", [])
            ][:100],
        }
        receipts.emit(
            logging.LogRecord("alerts", logging.INFO, "", 0, json.dumps(receipt), (), None)
        )
        return web.json_response({"received": True})

    async def background(application: web.Application):
        async def poll() -> None:
            while True:
                await asyncio.to_thread(observer.snapshot)
                await observer.containers()
                await observer.memory()
                await observer.heartbeat()
                await asyncio.sleep(15)

        task = asyncio.create_task(poll())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        receipts.close()

    app.router.add_get("/metrics", metrics)
    app.router.add_get("/healthz", health)
    app.router.add_post("/alerts", alerts)
    app.cleanup_ctx.append(background)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=19464)
    args = parser.parse_args()
    web.run_app(application(args.data_root.resolve()), host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
