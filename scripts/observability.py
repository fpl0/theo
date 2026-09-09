"""Run and verify local observability without changing macOS services or shared VM settings."""

import argparse
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "observability"
STATE = ROOT / ".local/observability"
CONTEXT = os.getenv("THEO_DOCKER_CONTEXT", "colima-theo-observability")
COMPOSE = ["docker", "--context", CONTEXT, "compose", "-f", str(STACK / "compose.yaml")]
URLS = {
    "grafana": "http://127.0.0.1:13000/api/health",
    "alloy": "http://127.0.0.1:12345/-/ready",
    "prometheus": "http://127.0.0.1:19090/-/ready",
    "loki": "http://127.0.0.1:13100/ready",
    "tempo": "http://127.0.0.1:13200/ready",
    "observer": "http://127.0.0.1:19464/healthz",
}


def credentials():
    path = STACK / ".env"
    if not path.exists():
        path.write_text("GRAFANA_ADMIN_PASSWORD=" + secrets.token_urlsafe(32) + "\n")
        path.chmod(0o600)
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )


def process(name, command, env=None):
    profile = hashlib.sha256(
        json.dumps(
            {k: v for k, v in (env or {}).items() if k.startswith("THEO_")}, sort_keys=True
        ).encode()
    ).hexdigest()
    record = STATE / (name + ".pid.json")
    if record.exists():
        saved = json.loads(record.read_text())
        try:
            old = psutil.Process(saved["pid"])
            if old.create_time() == saved["created_at"] and old.is_running():
                if saved.get("command") == command and (
                    name != "observer" or saved.get("profile") == profile
                ):
                    return saved["pid"]
                if name != "observer":
                    raise SystemExit(
                        "A managed native core is already using a different data root."
                    )
                stop(name)
        except psutil.Error:
            pass
    with (STATE / (name + ".log")).open("ab") as log:
        child = subprocess.Popen(
            command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True
        )
    record.write_text(
        json.dumps(
            {
                "pid": child.pid,
                "created_at": psutil.Process(child.pid).create_time(),
                "command": command,
                "profile": profile,
            }
        )
    )
    record.chmod(0o600)
    return child.pid


def stop(name):
    record = STATE / (name + ".pid.json")
    if not record.exists():
        return
    saved = json.loads(record.read_text())
    try:
        old = psutil.Process(saved["pid"])
        if old.create_time() == saved["created_at"]:
            old.send_signal(signal.SIGTERM)
            old.wait(timeout=15)
    except psutil.NoSuchProcess:
        pass
    record.unlink(missing_ok=True)


def health():
    result = {}
    for name, url in URLS.items():
        try:
            response = httpx.get(url, timeout=3)
            result[name] = {"ready": response.status_code == 200, "status": response.status_code}
        except httpx.HTTPError as exc:
            result[name] = {"ready": False, "error": type(exc).__name__}
    return result


def check():
    from opentelemetry import trace

    from theo.observability import telemetry

    os.environ["THEO_TRACE_SAMPLE_RATIO"] = "1"
    telemetry.configure(STATE / "canary", force=True)
    with telemetry.operation("qualification.pipeline", channel="cli"):
        telemetry.event("qualification.correlation", run_id="observability-canary")
        telemetry.measure("theo_qualification_duration", 0.125, histogram=True, channel="cli")
        trace_id = format(trace.get_current_span().get_span_context().trace_id, "032x")
    telemetry.shutdown()
    deadline = time.monotonic() + 45
    evidence = {"trace_id": trace_id}
    while time.monotonic() < deadline:
        tr = httpx.get("http://127.0.0.1:13200/api/traces/" + trace_id, timeout=5)
        log = httpx.get(
            "http://127.0.0.1:13100/loki/api/v1/query_range",
            params={
                "query": '{service_name="theo"} | trace_id="' + trace_id + '"',
                "start": str(int((time.time() - 300) * 1e9)),
                "limit": 20,
            },
            timeout=10,
        )
        ex = httpx.get(
            "http://127.0.0.1:19090/api/v1/query_exemplars",
            params={
                "query": "theo_qualification_duration_seconds_bucket",
                "start": time.time() - 300,
                "end": time.time(),
            },
            timeout=5,
        )
        evidence.update(
            trace_stored=tr.status_code == 200,
            logs_correlated=log.status_code == 200
            and bool(log.json().get("data", {}).get("result")),
            exemplar_correlated=trace_id in ex.text,
        )
        if all(evidence[k] for k in ("trace_stored", "logs_correlated", "exemplar_correlated")):
            break
        time.sleep(2)
    queries = 0
    log_queries = 0
    errors = []
    configured = httpx.get("http://127.0.0.1:13000/api/datasources/uid/tempo", timeout=5).json()[
        "jsonData"
    ]["tracesToLogsV2"]
    trace_query = (
        configured.get("query", "")
        .replace("${__tags}", 'deployment_environment_name="local"')
        .replace("${__trace.traceId}", trace_id)
    )
    linked = httpx.get(
        "http://127.0.0.1:13100/loki/api/v1/query_range",
        params={"query": trace_query, "limit": 5},
        timeout=10,
    )
    evidence["grafana_trace_to_logs_correlated"] = (
        configured.get("customQuery") is True
        and linked.status_code == 200
        and bool(linked.json().get("data", {}).get("result"))
    )
    if not evidence["grafana_trace_to_logs_correlated"]:
        errors.append({"correlation": "Grafana trace-to-logs query did not find the trace"})
    for path in (STACK / "grafana/dashboards").glob("*.json"):
        for panel in json.loads(path.read_text())["panels"]:
            source = panel.get("datasource", {}).get("uid")
            if source not in {"prometheus", "loki"}:
                continue
            for target in panel["targets"]:
                if source == "loki" and "$__rate_interval" in target["expr"]:
                    errors.append(
                        {
                            "dashboard": path.name,
                            "panel": panel["title"],
                            "error": "Loki cannot interpolate Prometheus $__rate_interval",
                        }
                    )
                    continue
                expr = (
                    target["expr"]
                    .replace("$__rate_interval", "5m")
                    .replace("$__range", "6h")
                    .replace("$environment", "local")
                )
                if source == "prometheus":
                    response = httpx.get(
                        "http://127.0.0.1:19090/api/v1/query", params={"query": expr}, timeout=10
                    )
                    queries += 1
                else:
                    response = httpx.get(
                        "http://127.0.0.1:13100/loki/api/v1/query_range",
                        params={"query": expr, "start": int((time.time() - 300) * 1e9), "limit": 5},
                        timeout=10,
                    )
                    log_queries += 1
                if response.status_code != 200:
                    errors.append(
                        {"dashboard": path.name, "panel": panel["title"], "error": response.text}
                    )
    readiness = health()
    qualification = STATE / "memory-qualification.json"
    evidence.update(
        health=readiness,
        dashboard_queries=queries,
        log_queries=log_queries,
        query_errors=errors,
        whole_stack_memory_verified=qualification.exists()
        and json.loads(qualification.read_text()).get("passed") is True,
    )
    rule_queries = 0
    for group in json.loads((STACK / "grafana/provisioning/alerting/rules.yaml").read_text())[
        "groups"
    ]:
        for rule in group["rules"]:
            response = httpx.get(
                "http://127.0.0.1:19090/api/v1/query",
                params={"query": rule["data"][0]["model"]["expr"]},
                timeout=10,
            )
            rule_queries += 1
            if response.status_code != 200:
                errors.append({"rule": rule["uid"], "error": response.text})
            if rule["uid"] == "theo-telegram-poller":
                cases = json.loads((STACK / "tests/poller-cases.json").read_text())
                fixture = {
                    "evaluation_interval": "1m",
                    "tests": [
                        {
                            "name": case["name"],
                            "interval": "1m",
                            "input_series": case["input_series"],
                            "promql_expr_test": [
                                {
                                    "expr": "(" + rule["data"][0]["model"]["expr"] + ") > 0",
                                    "eval_time": "10m",
                                    "exp_samples": [{"labels": "{}", "value": 1}]
                                    if case["should_alert"]
                                    else [],
                                }
                            ],
                        }
                        for case in cases
                    ],
                }
                tested = subprocess.run(
                    COMPOSE
                    + ["exec", "-T", "prometheus", "promtool", "test", "rules", "/dev/stdin"],
                    input=json.dumps(fixture),
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                evidence["poller_alert_behavior"] = {
                    "cases": len(cases),
                    "passed": tested.returncode == 0,
                }
                if tested.returncode:
                    errors.append(
                        {"rule": rule["uid"], "error": (tested.stdout + tested.stderr)[-3000:]}
                    )
    evidence["alert_queries"] = rule_queries
    (STATE / "validation.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    if (
        errors
        or not all(value["ready"] for value in readiness.values())
        or not all(evidence[k] for k in ("trace_stored", "logs_correlated", "exemplar_correlated"))
    ):
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["up", "down", "status", "check", "alert-on", "alert-off"]
    )
    parser.add_argument("--data-root", type=Path, default=STATE / "theo")
    parser.add_argument(
        "--with-test-core",
        action="store_true",
        help="Run a separate native core using this explicitly isolated data root",
    )
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    data = args.data_root.resolve()
    if args.action == "up":
        private = credentials()
        subprocess.run([sys.executable, str(ROOT / "scripts/build_observability.py")], check=True)
        subprocess.run(COMPOSE + ["up", "-d"], check=True)
        if args.with_test_core:
            if not (data / "config.json").exists():
                subprocess.run(
                    [sys.executable, "-m", "theo", "--data-root", str(data), "init"], check=True
                )
            process(
                "core",
                [sys.executable, "-m", "theo", "--data-root", str(data), "serve"],
                {**os.environ, "THEO_TELEMETRY_ENABLED": "1", "THEO_TRACE_SAMPLE_RATIO": "1"},
            )
        if not data.exists():
            raise SystemExit(
                "The data root must already exist, or pass --with-test-core for a new local test instance."
            )
        process(
            "observer",
            [sys.executable, "-m", "theo.observability.observer", "--data-root", str(data)],
            {
                **os.environ,
                "THEO_DOCKER_CONTEXT": CONTEXT,
                "THEO_OBSERVABILITY_QUALIFICATION": str(STATE / "memory-qualification.json"),
                "THEO_HEARTBEAT_URL": os.getenv(
                    "THEO_HEARTBEAT_URL", private.get("THEO_HEARTBEAT_URL", "")
                ),
            },
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if all(value["ready"] for value in health().values()):
                break
            time.sleep(2)
        else:
            raise SystemExit(
                "Observability readiness timed out; run the status command and inspect Compose logs."
            )
        print("Grafana: http://127.0.0.1:13000/d/theo-overview")
    elif args.action == "down":
        stop("observer")
        stop("core")
        subprocess.run(COMPOSE + ["down"], check=True)
    elif args.action == "status":
        print(json.dumps(health(), indent=2))
    elif args.action == "check":
        check()
    elif args.action == "alert-on":
        (data / "telemetry-test-alert").touch()
    else:
        (data / "telemetry-test-alert").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
