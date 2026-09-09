"""Bounded synthetic telemetry load and macOS physical-memory qualification."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
from prometheus_client.parser import text_string_to_metric_families

from theo.observability import telemetry

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--rate", type=int, default=10)
    args = parser.parse_args()
    os.environ["THEO_TRACE_SAMPLE_RATIO"] = "1"
    os.environ["THEO_ENVIRONMENT"] = "qualification"
    destination = ROOT / ".local/observability"
    telemetry.configure(destination / "load", "theo-qualification", force=True)
    start = time.monotonic()
    started_at = time.time()
    samples = []
    operations = 0
    errors = []
    last_sample = -15
    with httpx.Client(timeout=5) as client:
        while time.monotonic() - start < args.seconds:
            elapsed = time.monotonic() - start
            burst = args.seconds / 3 <= elapsed < args.seconds / 3 + 30
            count = args.rate * (10 if burst else 1)
            loop_start = time.monotonic()
            for index in range(count):
                with telemetry.operation(
                    "qualification.load", channel="cli", tool="fixture_" + str(index % 20)
                ):
                    telemetry.event("qualification.load")
                    telemetry.measure(
                        "theo_qualification_load_duration", 0.1, histogram=True, channel="cli"
                    )
                operations += 1
            if elapsed - last_sample >= 15:
                last_sample = elapsed
                try:
                    response = client.get("http://127.0.0.1:19464/metrics")
                    values = {
                        sample.name: sample.value
                        for family in text_string_to_metric_families(response.text)
                        for sample in family.samples
                        if sample.name
                        in {
                            "theo_observability_memory_bytes",
                            "theo_observability_memory_measurement_available",
                            "theo_host_swap_bytes",
                        }
                    }
                    values.update(elapsed=round(elapsed, 2), operations=operations, burst=burst)
                    samples.append(values)
                    query = client.get(
                        "http://127.0.0.1:19090/api/v1/query",
                        params={
                            "query": "histogram_quantile(0.95,sum by(le,operation)(rate(theo_operation_duration_seconds_bucket[5m])))"
                        },
                    )
                    if query.status_code != 200:
                        errors.append("Prometheus query failed")
                    traces = client.get("http://127.0.0.1:13200/api/search", params={"limit": 5})
                    if traces.status_code != 200:
                        errors.append("Tempo search failed")
                except httpx.HTTPError as exc:
                    errors.append(type(exc).__name__)
                if int(elapsed) % 60 < 15:
                    print(json.dumps(samples[-1] if samples else {"elapsed": elapsed}), flush=True)
            time.sleep(max(0, 1 - (time.monotonic() - loop_start)))
    telemetry.shutdown()
    inspected = subprocess.check_output(
        [
            "docker",
            "--context",
            "colima-theo-observability",
            "ps",
            "-aq",
            "--filter",
            "label=com.docker.compose.project=theo-observability",
        ],
        text=True,
    ).split()
    states = json.loads(
        subprocess.check_output(
            ["docker", "--context", "colima-theo-observability", "inspect", *inspected], text=True
        )
    )
    memory = [
        x["theo_observability_memory_bytes"]
        for x in samples
        if x.get("theo_observability_memory_measurement_available") == 1
    ]
    report = {
        "started_at": started_at,
        "finished_at": time.time(),
        "duration_seconds": time.monotonic() - start,
        "operations": operations,
        "samples": samples,
        "errors": errors,
        "peak_physical_footprint_bytes": max(memory) if memory else None,
        "budget_bytes": 2_000_000_000,
        "container_restarts": {c["Name"]: c["RestartCount"] for c in states},
        "oom_killed": any(c["State"]["OOMKilled"] for c in states),
        "method": "macOS footprint sum: all Apple VZ helpers, Colima Lima/SSH helpers, native observer; guest Linux/Docker/containers included in VZ footprint. Shared VZ helpers are conservatively overcounted.",
    }
    events = subprocess.check_output(
        [
            "docker",
            "--context",
            "colima-theo-observability",
            "events",
            "--since",
            str(int(started_at)),
            "--until",
            str(int(time.time())),
            "--filter",
            "label=com.docker.compose.project=theo-observability",
            "--filter",
            "event=oom",
            "--format",
            "{{.Actor.ID}}",
        ],
        text=True,
    ).splitlines()
    report["oom_events"] = len(events)
    report["passed"] = bool(
        len(memory) >= 10
        and max(memory) < 2_000_000_000
        and not errors
        and not report["oom_killed"]
        and not events
        and all(c["RestartCount"] == 0 for c in states)
        and all(x.get("theo_host_swap_bytes", 0) == 0 for x in samples)
    )
    (destination / "memory-qualification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "samples"}, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
