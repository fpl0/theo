"""Bounded local collector outage: prove native progress, queue loss visibility and recovery."""

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import psutil
from opentelemetry import trace

from theo.observability import telemetry

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ["docker", "--context", "colima-theo-observability"]


def main():
    os.environ.update(THEO_TRACE_SAMPLE_RATIO="1", THEO_ENVIRONMENT="qualification")
    root = ROOT / ".local/observability/fault"
    telemetry.configure(root, "theo-qualification", force=True)
    process = psutil.Process()
    before = process.memory_info().rss
    report = {"operations": 3000, "started_at": time.time()}
    try:
        subprocess.run(
            DOCKER + ["stop", "--time", "10", "theo-observability-alloy-1"],
            check=True,
            capture_output=True,
        )
        start = time.monotonic()
        for _ in range(report["operations"]):
            with telemetry.operation("qualification.collector_outage", channel="cli"):
                telemetry.event("qualification.native_progress")
        report["native_progress_seconds"] = time.monotonic() - start
        report["native_rss_growth_bytes"] = process.memory_info().rss - before
        time.sleep(3)
    finally:
        subprocess.run(
            DOCKER + ["start", "theo-observability-alloy-1"], check=True, capture_output=True
        )
    with httpx.Client(timeout=5) as client:
        for _ in range(30):
            try:
                if client.get("http://127.0.0.1:12345/-/ready").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        with telemetry.operation("qualification.recovered", channel="cli"):
            report["recovered_trace_id"] = (
                f"{trace.get_current_span().get_span_context().trace_id:032x}"
            )
        time.sleep(3)
        telemetry.shutdown()
        for _ in range(30):
            response = client.get(
                "http://127.0.0.1:13200/api/traces/" + report["recovered_trace_id"]
            )
            query = client.get(
                "http://127.0.0.1:19090/api/v1/query",
                params={
                    "query": 'sum({__name__=~"otel_sdk_processor_(span|log)_processed_total",error_type="queue_full",environment="qualification"})'
                },
            )
            data = query.json().get("data", {}).get("result", [])
            report["dropped_records_reported"] = float(data[0]["value"][1]) if data else 0
            report["trace_export_recovered"] = response.status_code == 200
            if report["trace_export_recovered"] and report["dropped_records_reported"] > 0:
                break
            time.sleep(1)
    report["passed"] = bool(
        report["native_progress_seconds"] < 10
        and report["native_rss_growth_bytes"] < 40_000_000
        and report["trace_export_recovered"]
        and report["dropped_records_reported"] > 0
    )
    (ROOT / ".local/observability/fault-qualification.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
