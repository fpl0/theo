import asyncio
import json
import re
import runpy
from pathlib import Path

import pytest

from theo.observability.budget import MEMORY_BUDGET_BYTES
from theo.observability.observer import Observer, alert_receipt

ROOT = Path(__file__).resolve().parents[1]
ALERTS = runpy.run_path(str(ROOT / "scripts/observability_alerts.py"))


def destination(route, labels):
    for child in route.get("routes", []):
        if all(
            labels.get(key, "") == value if op == "=" else re.fullmatch(value, labels.get(key, ""))
            for key, op, value in child.get("object_matchers", [])
        ):
            return destination(child, labels)
    return route["receiver"]


@pytest.mark.parametrize(
    "labels,expected",
    [
        ({"environment": "local", "severity": "critical"}, "Theo local diagnostics"),
        ({"severity": "warning"}, "Theo local diagnostics"),
        ({"environment": "qualification", "severity": "critical"}, "Theo local diagnostics"),
        (
            {"environment": "production", "severity": "critical", "incident": "availability"},
            "Theo operational alerts",
        ),
        (
            {"environment": "production", "severity": "warning", "incident": "model-access"},
            "Theo operational alerts",
        ),
        ({"environment": "local", "severity": "test"}, "Theo operational alerts"),
    ],
)
def test_only_production_and_explicit_tests_notify(labels, expected):
    config = ALERTS["contact_points"](True)
    assert destination(config["policies"][0], labels) == expected
    diagnostics = next(c for c in config["contactPoints"] if c["name"] == "Theo local diagnostics")
    assert all(r["type"] == "webhook" for r in diagnostics["receivers"])


def test_incident_grouping_keeps_related_alerts_together_and_hosts_separate():
    policy = ALERTS["contact_points"](True)["policies"][0]
    base = {"environment": "production", "host": "server-a", "incident": "availability"}
    alerts = [
        {**base, "alertname": "core", "severity": "critical"},
        {**base, "alertname": "poller", "severity": "warning"},
        {**base, "host": "server-b", "alertname": "core"},
    ]
    keys = [tuple(a[k] for k in policy["group_by"]) for a in alerts]
    assert keys[0] == keys[1] and keys[0] != keys[2]


def test_inventory_unknown_does_not_become_recovery_and_monitor_failure_still_alerts():
    rules = ALERTS["alert_rules"]()
    by_id = {r["uid"]: r for r in rules["groups"][0]["rules"]}
    for uid in (
        "codex-auth",
        "codex-quota",
        "uncertain-delivery",
        "delivery-stalled",
        "core-unavailable",
    ):
        rule = by_id["theo-" + uid]
        assert rule["noDataState"] == "KeepLast"
        assert rule["keepFiringFor"] == "2m"
    assert by_id["theo-observer-missing"]["execErrState"] == "Alerting"
    assert by_id["theo-observer-stale"]["noDataState"] == "Alerting"
    assert sum("waiting_for_auth" in r["data"][0]["model"]["expr"] for r in by_id.values()) == 1
    assert {"orgId": 1, "uid": "theo-codex-evidence-expiring"} in rules["deleteRules"]


def test_receipts_preserve_missing_series_reason_without_message_or_error_text():
    receipt = alert_receipt(
        {
            "status": "resolved",
            "message": "private-message",
            "alerts": [
                {
                    "status": "resolved",
                    "fingerprint": "123",
                    "labels": {
                        "host": "fixture.local",
                        "environment": "local",
                        "incident": "availability",
                    },
                    "annotations": {
                        "grafana_state_reason": "MissingSeries",
                        "description": "private-description",
                    },
                },
                {"annotations": {"grafana_state_reason": "private-error"}},
            ],
        }
    )
    assert receipt["alerts"][0]["state_reason"] == "MissingSeries"
    assert receipt["alerts"][0]["host"] == "fixture.local"
    assert receipt["alerts"][1]["state_reason"] == "Other"
    assert "private-" not in json.dumps(receipt)


async def test_observer_marks_partial_refresh_unknown_and_rejects_old_budget_evidence(
    db, monkeypatch
):
    observer = Observer(db.root)
    seen = []
    original = observer.set

    def capture(name, value, **labels):
        if name == "theo_core_ready":
            seen.append(
                observer.registry.get_sample_value(
                    "theo_database_readable", {"environment": "local"}
                )
            )
        original(name, value, **labels)

    monkeypatch.setattr(observer, "set", capture)
    report = db.root / "historical-budget.json"
    report.write_text(json.dumps({"passed": True, "budget_bytes": 2_000_000_000}))
    monkeypatch.setenv("THEO_OBSERVABILITY_QUALIFICATION", str(report))
    monkeypatch.setenv("THEO_DOCKER_CONTEXT", "colima-theo-observability")
    await asyncio.to_thread(observer.snapshot)
    assert seen == [0]
    assert (
        observer.registry.get_sample_value("theo_database_readable", {"environment": "local"}) == 1
    )
    assert (
        observer.registry.get_sample_value(
            "theo_observability_budget_bytes", {"environment": "local"}
        )
        == MEMORY_BUDGET_BYTES
        == 4_000_000_000
    )
    assert (
        observer.registry.get_sample_value(
            "theo_observability_budget_verified", {"environment": "local"}
        )
        == 0
    )


def test_monitoring_deployment_does_not_change_supervisor_paths(tmp_path):
    module = runpy.run_path(str(ROOT / "scripts/deploy_services.py"))
    manifest = {
        "data_root": str(tmp_path),
        "source": "/core/source",
        "python": "/core/python",
        "observability_source": "/monitoring/source",
        "observability_python": "/monitoring/python",
    }
    supervisor = module["definition"](manifest, tmp_path / "manifest.json", "supervisor")
    observer = module["definition"](manifest, tmp_path / "manifest.json", "observer")
    assert supervisor["ProgramArguments"][0] == "/core/python"
    assert supervisor["WorkingDirectory"] == "/core/source"
    assert observer["ProgramArguments"][0] == "/monitoring/python"
    assert observer["WorkingDirectory"] == "/monitoring/source"
