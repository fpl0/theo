"""Generate incident-oriented alerts and notification routing without private data."""

import json

from theo.observability.budget import MEMORY_BUDGET_BYTES

LIVE = 'environment=~"local|production"'
READABLE = f"max by(environment)(theo_database_readable{{{LIVE}}}) == 1"


def observed(metric):
    return f"max by(environment)({metric}{{{LIVE}}})"


def inventory(status):
    # A missing status means zero only while the complete database snapshot is readable.
    return (
        f'(sum by(environment)(theo_jobs_current{{{LIVE},status="{status}"}})'
        f" or on(environment) (0 * ({READABLE}))) and on(environment) ({READABLE})"
    )


def definitions():
    configured = f'max by(environment)(theo_channel_configured{{{LIVE},channel="telegram"}})'
    poll = (
        f"max by(environment)(last_over_time(theo_telegram_poll_success_timestamp{{{LIVE}}}[24h]))"
    )
    poller = (
        f"(((time()-{poll} > bool 120) or ({configured} unless on(environment) {poll}))"
        f" and on(environment) ({configured}==1)) or on(environment) (0 * {configured})"
    )
    # uid, title, query, comparator, threshold, pending, severity, incident, action, unit
    return [
        (
            "core-unavailable",
            "Theo core unavailable",
            observed("theo_core_ready") + f" and on(environment) ({READABLE})",
            "lt",
            1,
            "2m",
            "critical",
            "availability",
            "Inspect the core heartbeat and supervisor recovery log on this host.",
            "ready (1=yes)",
        ),
        (
            "observer-missing",
            "Theo monitoring unavailable",
            'min(up{job="theo-observer"}) or vector(0)',
            "lt",
            1,
            "2m",
            "critical",
            "monitoring",
            "Check the observer and Prometheus. Existing incidents retain their last state while measurements are unavailable.",
            "observer reachable (1=yes)",
        ),
        (
            "observer-stale",
            "Theo monitoring snapshot stale",
            "time()-" + observed("theo_observer_refresh_timestamp_seconds"),
            "gt",
            60,
            "2m",
            "critical",
            "monitoring",
            "Inspect the observer refresh loop; a reachable endpoint can still serve old measurements.",
            "seconds since refresh",
        ),
        (
            "database-unreadable",
            "Theo monitoring cannot read state",
            observed("theo_database_readable"),
            "lt",
            1,
            "2m",
            "critical",
            "monitoring",
            "Check the observer's read-only database access. Missing inventory must not be interpreted as completed work.",
            "database readable (1=yes)",
        ),
        (
            "queue-stalled",
            "Theo queue stalled",
            observed("theo_queue_oldest_seconds"),
            "gt",
            300,
            "5m",
            "warning",
            "work",
            "Inspect queued jobs and deliberate pause controls.",
            "seconds queued",
        ),
        (
            "delivery-stalled",
            "Theo delivery stalled",
            observed("theo_outbox_oldest_seconds"),
            "gt",
            300,
            "5m",
            "warning",
            "delivery",
            "Inspect Telegram transport errors and the delivery ledger before retrying.",
            "seconds waiting",
        ),
        (
            "uncertain-delivery",
            "Theo uncertain delivery",
            observed("theo_actions_uncertain"),
            "gt",
            0,
            "1m",
            "warning",
            "delivery",
            "Reconcile the exact receipt; do not resend an uncertain delivery.",
            "uncertain actions",
        ),
        (
            "telegram-poller",
            "Theo Telegram poller stale",
            poller,
            "gt",
            0,
            "2m",
            "warning",
            "availability",
            "Check the core, network and bot credentials. Do not start a second poller.",
            "stale (1=yes)",
        ),
        (
            "codex-auth",
            "Theo model access requires attention",
            inventory("waiting_for_auth"),
            "gt",
            0,
            "1m",
            "warning",
            "model-access",
            "Inspect the waiting job's access error. Codex verifies login and included allowance automatically; resolve the reported condition, then explicitly retry the preserved job.",
            "blocked jobs",
        ),
        (
            "codex-quota",
            "Theo model allowance exhausted",
            inventory("waiting_for_quota"),
            "gt",
            0,
            "1m",
            "warning",
            "model-access",
            "Wait for the reported allowance reset, then explicitly retry the preserved job. No paid fallback.",
            "blocked jobs",
        ),
        (
            "disk-pressure",
            "Theo host disk pressure",
            observed("theo_host_disk_used_ratio"),
            "gt",
            0.85,
            "5m",
            "warning",
            "resources",
            "Inspect retention and disk use before storage fills.",
            "ratio of disk used",
        ),
        (
            "container-memory",
            "Theo container memory pressure",
            f"max by(environment,component)(theo_container_memory_bytes{{{LIVE}}}/theo_container_memory_limit_bytes{{{LIVE}}})",
            "gt",
            0.9,
            "5m",
            "warning",
            "resources",
            "Inspect this container's memory trend and OOM events. This is its own hard limit; the separate whole-stack budget is 4 GB.",
            "ratio of container limit",
        ),
        (
            "backend-missing",
            "Theo telemetry backend unavailable",
            'up{job="infrastructure"}',
            "lt",
            1,
            "2m",
            "critical",
            "monitoring",
            "Inspect the affected Compose service and its health endpoint.",
            "backend reachable (1=yes)",
        ),
        (
            "alert-test",
            "Theo notification delivery test",
            observed("theo_observability_test_alert"),
            "gt",
            0,
            "0s",
            "test",
            "notification-test",
            "Explicit operator notification test. No recovery action is required.",
            "test marker (1=active)",
        ),
        (
            "whole-memory",
            "Theo whole-stack memory budget exceeded",
            observed("theo_observability_memory_bytes")
            + f" and on(environment) ({observed('theo_observability_memory_measurement_available')}==1)",
            "gt",
            MEMORY_BUDGET_BYTES,
            "1m",
            "critical",
            "resources",
            "Inspect the Infrastructure dashboard. The 4 GB budget includes the VM, Docker, observer and host helpers.",
            "bytes / 4000000000 budget",
        ),
        (
            "memory-measurement",
            "Theo memory measurement unavailable",
            observed("theo_observability_memory_measurement_available"),
            "lt",
            1,
            "5m",
            "warning",
            "monitoring",
            "Inspect the observer's host-memory collection; the last footprint is not a current budget check.",
            "measurement available (1=yes)",
        ),
        (
            "container-restarted",
            "Theo telemetry container restarted",
            f"max by(environment,component)(delta(theo_container_restarts{{{LIVE}}}[5m]))",
            "gt",
            0,
            "0s",
            "warning",
            "monitoring",
            "Inspect the container's OOM events and logs. A recovered process can still have lost telemetry.",
            "restarts in 5 minutes",
        ),
        (
            "native-telemetry-stale",
            "Theo native telemetry stale",
            "time()-" + observed("theo_runtime_telemetry_timestamp"),
            "gt",
            120,
            "3m",
            "warning",
            "availability",
            "Check the core's telemetry configuration and Alloy export. Core availability and telemetry export are separate signals.",
            "seconds since telemetry",
        ),
        (
            "telemetry-loss",
            "Theo telemetry records dropped",
            'sum(increase({__name__=~"otel_sdk_processor_(span|log)_processed_total",error_type!=""}[5m]))',
            "gt",
            0,
            "1m",
            "warning",
            "monitoring",
            "Inspect bounded SDK buffers. Missing telemetry does not prove successful operations.",
            "dropped records in 5 minutes",
        ),
        (
            "collector-loss",
            "Theo collector export failures",
            'sum(increase(label_replace({__name__=~"otelcol_.*(refused|send_failed|enqueue_failed).*_total"},"signal","$1","__name__","(.*)")[5m:30s]))',
            "gt",
            0,
            "1m",
            "warning",
            "monitoring",
            "Inspect downstream backends and bounded collector queues.",
            "failed exports in 5 minutes",
        ),
        (
            "schedule-late",
            "Theo schedule overdue",
            observed("theo_schedule_overdue_seconds"),
            "gt",
            300,
            "5m",
            "warning",
            "work",
            "Inspect the scheduler and deliberate background pause controls.",
            "seconds overdue",
        ),
    ]


def alert_rules():
    rules = []
    database_rules = {"queue-stalled", "delivery-stalled", "uncertain-delivery", "schedule-late"}
    for (
        uid,
        title,
        query,
        comparator,
        threshold,
        period,
        severity,
        incident,
        action,
        unit,
    ) in definitions():
        if uid in database_rules:
            query += f" and on(environment) ({READABLE})"
        # B retains the actual measurement for notifications; A remains the boolean condition.
        operator = "<" if comparator == "lt" else ">"
        expression = f"({query}) {operator} bool {threshold}"
        no_data = (
            "Alerting"
            if uid
            in {
                "observer-missing",
                "observer-stale",
                "database-unreadable",
                "native-telemetry-stale",
                "memory-measurement",
            }
            else "KeepLast"
        )
        measurement = (
            '{{ with $values.B }}{{ printf "%.3g" .Value }}{{ else }}unavailable{{ end }} ' + unit
        )
        if uid in {"disk-pressure", "container-memory"}:
            measurement = (
                "{{ with $values.B }}{{ humanizePercentage .Value }}{{ else }}unavailable{{ end }} "
                + unit.removeprefix("ratio ")
                + f" (threshold: {threshold:.0%})"
            )
        if uid == "whole-memory":
            measurement = "{{ with $values.B }}{{ humanize .Value }}B{{ else }}unavailable{{ end }} / 4 GB whole-stack budget"
        rules.append(
            {
                "uid": "theo-" + uid,
                "title": title,
                "condition": "C",
                "for": period,
                "keepFiringFor": "0s" if severity == "test" else "2m",
                "missingSeriesEvalsToResolve": 20,
                "noDataState": no_data,
                "execErrState": "Alerting" if uid == "observer-missing" else "KeepLast",
                "annotations": {
                    "summary": title,
                    "description": action,
                    "measurement": measurement,
                },
                "labels": {
                    "service": "theo",
                    "severity": severity,
                    "incident": incident,
                    "host": "$THEO_ALERT_HOST",
                    "environment": "{{ if $$labels.environment }}{{ $$labels.environment }}{{ else }}$THEO_ENVIRONMENT{{ end }}",
                },
                "data": [
                    {
                        "refId": ref,
                        "relativeTimeRange": {"from": 600, "to": 0},
                        "datasourceUid": "prometheus",
                        "model": {
                            "refId": ref,
                            "expr": expr,
                            "instant": True,
                            "range": False,
                            "intervalMs": 1000,
                            "maxDataPoints": 43200,
                        },
                    }
                    for ref, expr in (("A", expression), ("B", query))
                ]
                + [
                    {
                        "refId": "C",
                        "relativeTimeRange": {"from": 0, "to": 0},
                        "datasourceUid": "__expr__",
                        "model": {
                            "refId": "C",
                            "type": "threshold",
                            "expression": "A",
                            "conditions": [
                                {
                                    "evaluator": {"type": "gt", "params": [0]},
                                    "operator": {"type": "and"},
                                    "reducer": {"type": "last", "params": []},
                                    "type": "query",
                                }
                            ],
                        },
                    }
                ],
            }
        )
    return {
        "apiVersion": 1,
        "deleteRules": [{"orgId": 1, "uid": "theo-codex-evidence-expiring"}],
        "groups": [
            {"orgId": 1, "name": "Theo health", "folder": "Theo", "interval": "30s", "rules": rules}
        ],
    }


MESSAGE = """[$THEO_ALERT_LABEL] {{ .CommonLabels.host }} · {{ .CommonLabels.incident }}
{{ if .Alerts.Firing }}ACTIVE: {{ len .Alerts.Firing }}
{{ range .Alerts.Firing }}• [{{ .Labels.severity | toUpper }}] {{ .Annotations.summary }}{{ with .Labels.component }} — {{ . }}{{ end }}{{ with .Labels.instance }} — {{ . }}{{ end }}
  {{ .Annotations.measurement }}
  {{ .Annotations.description }}
{{ end }}{{ end }}{{ if .Alerts.Resolved }}{{ range .Alerts.Resolved }}{{ if .Annotations.grafana_state_reason }}STATE CHANGED: {{ .Annotations.summary }} ({{ .Annotations.grafana_state_reason }}; recovery not verified)
{{ else }}RECOVERED: {{ .Annotations.summary }}{{ with .Labels.component }} — {{ . }}{{ end }}
{{ end }}{{ end }}{{ end }}{{ reReplaceAll "/$" "" .ExternalURL }}/d/theo-overview?var-environment={{ .CommonLabels.environment }}"""


def contact_points(telegram_enabled):
    receipt = {
        "uid": "theo-local-receipts",
        "type": "webhook",
        "settings": {"url": "http://host.docker.internal:19464/alerts", "httpMethod": "POST"},
        "disableResolveMessage": False,
    }
    operational = {**receipt, "uid": "theo-operational-receipts"}
    receivers = [operational]
    if telegram_enabled:
        receivers.append(
            {
                "uid": "theo-telegram-test",
                "type": "telegram",
                "settings": {
                    "bottoken": "$THEO_ALERT_BOT_TOKEN",
                    "chatid": "$THEO_ALERT_CHAT_ID",
                    "parse_mode": "",
                    "disable_web_page_preview": True,
                    "message": MESSAGE,
                },
                "disableResolveMessage": False,
            }
        )
    return {
        "apiVersion": 1,
        "contactPoints": [
            {"orgId": 1, "name": "Theo local diagnostics", "receivers": [receipt]},
            {"orgId": 1, "name": "Theo operational alerts", "receivers": receivers},
        ],
        "policies": [
            {
                "orgId": 1,
                "receiver": "Theo local diagnostics",
                "group_by": ["environment", "host", "incident"],
                "group_wait": "1m",
                "group_interval": "5m",
                "repeat_interval": "12h",
                "routes": [
                    {
                        "receiver": "Theo operational alerts",
                        "object_matchers": [["severity", "=", "test"]],
                        "group_wait": "10s",
                        "group_interval": "1m",
                        "repeat_interval": "12h",
                    },
                    {
                        "receiver": "Theo operational alerts",
                        "object_matchers": [["environment", "=", "production"]],
                        "routes": [
                            {
                                "receiver": "Theo operational alerts",
                                "object_matchers": [
                                    ["incident", "=~", "availability|delivery|monitoring"]
                                ],
                                "group_wait": "30s",
                                "repeat_interval": "4h",
                            }
                        ],
                    },
                ],
            }
        ],
    }


def build_alerting(root, telegram_enabled):
    for name, value in (
        ("rules", alert_rules()),
        ("contact-points", contact_points(telegram_enabled)),
    ):
        (root / "provisioning/alerting" / (name + ".yaml")).write_text(
            json.dumps(value, indent=2) + "\n"
        )
