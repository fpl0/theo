"""Generate the provisioned Theo dashboards and Grafana alerts deterministically."""

import json
import os
import re
from html import escape
from pathlib import Path

from observability_alerts import build_alerting

from theo.observability.budget import MEMORY_BUDGET_BYTES

ROOT = Path(__file__).resolve().parents[1] / "observability/grafana"
P = {"type": "prometheus", "uid": "prometheus"}
LINKS = [
    ("theo-overview", "Overview"),
    ("theo-jobs", "Jobs & Tools"),
    ("theo-telegram", "Telegram"),
    ("theo-cli", "CLI"),
    ("theo-codex", "Codex"),
    ("theo-infrastructure", "Infrastructure"),
]
ZERO = " or on() (0 * (max(theo_database_readable) == 1))"


def color_overrides():
    return [
        {
            "matcher": {"id": "byRegexp", "options": pattern},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}],
        }
        for pattern, color in [
            (".*(telegram|loki).*", "#56D2D5"),
            (".*(cli|alloy|job.run).*", "#5794F2"),
            (".*(codex|ai.run|tempo).*", "#B877D9"),
            (".*grafana.*", "#FFB357"),
            (".*prometheus.*", "#73BF69"),
            (".*(telegram.receive|cli.connect).*", "#56D2D5"),
            (".*(telegram.process|cli.submit).*", "#5794F2"),
            (".*(telegram.send|delivery.send).*", "#73BF69"),
            (".*telegram.poll_once.*", "#FFB357"),
            (".*telegram.hydrate.*", "#B877D9"),
            (".*(success|succeeded|completed|done|healthy).*", "#73BF69"),
            (".*(queued|pending|waiting|retry|quota|auth).*", "#FFB357"),
            (".*(error|failed|uncertain).*", "#F2495C"),
            (".*(cancelled|interrupted).*", "#8E8EA4"),
        ]
    ]


def polish(dashboard):
    """Keep the native Grafana language consistent and give every empty state context."""
    guides = {
        "theo-overview": "Start with active alerts and blocked work. Blue numbers are measurements; amber needs attention; red indicates failure. No samples means no matching observations, not zero errors. Expand a log line to follow its trace.",
        "theo-jobs": "Inventory is durable state, including completed history. A blocked job needs its cause resolved; it is not ordinary queue delay. Tool latency is p95 over the displayed sampling window. Expand a job or tool event to follow its trace.",
        "theo-telegram": "A long poll can wait about 30 seconds by design; it is excluded from processing latency. Delivery success means Telegram acknowledged the message. Uncertain delivery must be reconciled before retrying. No recent polling signal needs investigation even when the bot is configured.",
        "theo-cli": "Live Theo shows real terminal activity; Test fixtures shows isolated validation traffic. No samples is expected before a terminal session emits an observation. CLI log charts use five-minute windows and include short-lived sessions. Native host availability is reported under Live Theo.",
        "theo-codex": "Account verification is required before inference. Empty usage and allowance charts stay empty until Codex reports them; they never imply free usage or zero allowance. Work inventory is persisted over the last 24 hours. Use logs to distinguish authentication, quota and execution failures.",
        "theo-infrastructure": "The 4 GB limit includes the Docker VM, observer and host helpers. Container memory is already inside that total. Native Theo/Codex memory is separate. Load test result refers to the last completed qualification; use live memory and headroom for the current state. An unconfigured external monitor cannot detect a whole-laptop outage.",
    }
    # Logs remain at the end so important operating signals are not buried below a log wall.
    ordered = [p for p in dashboard.panels if p["type"] != "logs"]
    log_panels = [p for p in dashboard.panels if p["type"] == "logs"]
    groups = {}
    for panel in ordered:
        groups.setdefault(panel["gridPos"]["y"], []).append(panel)
    y = 0
    for row in groups.values():
        for panel in row:
            panel["gridPos"]["y"] = y
        y += max(p["gridPos"]["h"] for p in row)
    dashboard.panels = ordered
    dashboard.y = y
    dashboard.add(
        "text",
        "Reading the signals",
        24,
        2,
        options={
            "mode": "html",
            "content": '<p style="font-size:13px;line-height:1.5;color:#b8bdc9;margin:4px 0"><strong>Reading the signals</strong> · '
            + escape(guides[dashboard.uid])
            + "</p>",
        },
    )
    dashboard.y += 2
    for panel in log_panels:
        panel["gridPos"]["y"] = dashboard.y
        dashboard.y += panel["gridPos"]["h"]
        dashboard.panels.append(panel)
    for index, panel in enumerate(dashboard.panels, 1):
        panel["id"] = index
        title = panel["title"]
        defaults = panel["fieldConfig"]["defaults"]
        if not panel.get("description") and panel["type"] not in {"text", "logs", "alertlist"}:
            panel["description"] = (
                "Latest observation for the selected traffic. Missing data is not zero. "
                if panel["type"] == "stat"
                else "Observed values over the selected time range. Gaps mean no samples. "
            ) + (
                "p95 is the duration below which 95% of sampled operations fall. "
                if "p95" in title or "latency" in title.lower()
                else ""
            )
        if panel["type"] == "stat":
            defaults["mappings"].append(
                {
                    "type": "special",
                    "options": {"match": "null", "result": {"text": "No samples", "color": "text"}},
                }
            )
            if defaults["unit"] == "short":
                defaults["decimals"] = 0
            if defaults["unit"] == "dateTimeAsIso":
                panel["options"]["text"]["valueSize"] = 22
            if title in {"Usage available", "Core ready", "Core available", "Configured"}:
                mappings = defaults["mappings"][0]["options"]
                if title == "Usage available":
                    mappings["0"].update(text="Awaiting run", color="text")
                if title == "Configured":
                    mappings["1"].update(color="blue")
            if title == "Account attestation":
                defaults["mappings"][0]["options"]["0"].update(
                    text="Needs verification", color="orange"
                )
            if title in {"Stack memory", "Whole observability stack"}:
                defaults.update(unit="decbytes", decimals=2)
                defaults["thresholds"]["steps"] = [
                    {"value": None, "color": "green"},
                    {"value": int(MEMORY_BUDGET_BYTES * 0.9), "color": "orange"},
                    {"value": MEMORY_BUDGET_BYTES, "color": "red"},
                ]
                panel["description"] = (
                    "Whole physical footprint: Docker VM, host helpers and native observer. Budget: 4.00 GB decimal. Amber at 90%, red at the budget."
                )
            if title in {
                "Uncertain actions",
                "Waiting for auth/quota",
                "Waiting jobs",
                "Approvals",
                "Awaiting approval",
            }:
                defaults["thresholds"]["steps"] = [
                    {"value": None, "color": "text"},
                    {"value": 1, "color": "orange"},
                ]
            if title in {"Last poll age", "Telemetry freshness"}:
                defaults["thresholds"]["steps"] = [
                    {"value": None, "color": "green"},
                    {"value": 60, "color": "orange"},
                    {"value": 120, "color": "red"},
                ]
                defaults["noValue"] = "No recent signal"
            destination = (
                "theo-codex"
                if title in {"Waiting for auth/quota", "Account attestation", "Usage available"}
                else "theo-jobs"
                if title
                in {
                    "Waiting jobs",
                    "Running jobs",
                    "Uncertain actions",
                    "Approvals",
                    "Awaiting approval",
                }
                else "theo-infrastructure"
                if title in {"Core ready", "Core available", "Stack memory"}
                else None
            )
            if destination:
                defaults["links"] = [
                    {
                        "title": "Open diagnostics",
                        "url": "/d/"
                        + destination
                        + "?var-environment=${environment}&${__url_time_range}",
                        "targetBlank": False,
                    }
                ]
        if panel["type"] == "timeseries" and defaults["unit"] == "percentunit":
            defaults["custom"].update(axisSoftMax=1)
        if title == "Whole stack · 4 GB ceiling":
            defaults["custom"].update(
                axisSoftMax=int(MEMORY_BUDGET_BYTES * 1.05), thresholdsStyle={"mode": "line"}
            )
            defaults["thresholds"]["steps"] = [
                {"value": None, "color": "green"},
                {"value": int(MEMORY_BUDGET_BYTES * 0.9), "color": "orange"},
                {"value": MEMORY_BUDGET_BYTES, "color": "red"},
            ]
        for metric, label in {
            "theo_core_memory_bytes": "Theo core",
            "theo_observer_memory_bytes": "Observer",
            "theo_queue_oldest_seconds": "Queued work",
            "theo_telegram_lag_seconds": "Telegram intake",
            "theo_outbox_oldest_seconds": "Delivery queue",
        }.items():
            panel["fieldConfig"]["overrides"].append(
                {
                    "matcher": {"id": "byName", "options": metric},
                    "properties": [{"id": "displayName", "value": label}],
                }
            )
        if panel["type"] == "bargauge":
            for state, label in {
                "waiting_for_auth": "Needs authentication",
                "waiting_for_quota": "Waiting for allowance",
                "completed": "Completed",
                "cancelled": "Cancelled",
                "queued": "Queued",
                "running": "Running",
                "uncertain": "Uncertain",
                "done": "Processed",
                "failed": "Failed",
                "succeeded": "Acknowledged",
            }.items():
                panel["fieldConfig"]["overrides"].append(
                    {
                        "matcher": {"id": "byName", "options": state},
                        "properties": [{"id": "displayName", "value": label}],
                    }
                )
        panel["title"] = {
            "Uncertain actions": "Uncertain",
            "Last completion": "Last AI result",
            "Total budget verified": "Load test result",
            "Waiting for auth/quota": "Blocked",
            "Account attestation": "Account verified",
        }.get(title, title)


class Dashboard:
    def __init__(self, uid, title, subtitle):
        self.uid, self.title, self.panels, self.y = uid, title, [], 0
        self.add(
            "text",
            title,
            24,
            2,
            options={
                "mode": "html",
                "content": f'<h2 style="font-size:22px;margin:0 0 6px;font-weight:600">{escape(title)}</h2><p style="font-size:13px;color:#b8bdc9;margin:0">{escape(subtitle)}</p>',
            },
        )
        self.y = 2

    def add(
        self,
        kind,
        title,
        w,
        h,
        *,
        x=0,
        expr=None,
        unit="short",
        description="",
        options=None,
        thresholds=None,
    ):
        panel = {
            "id": len(self.panels) + 1,
            "type": kind,
            "title": "" if kind == "text" else title,
            "description": description,
            "gridPos": {"x": x, "y": self.y, "w": w, "h": h},
            "transparent": kind == "text",
            "fieldConfig": {
                "defaults": {
                    "unit": unit,
                    "noValue": "No samples",
                    "mappings": (
                        [{"type": "value", "options": {"0": {"text": "Not yet", "color": "text"}}}]
                        if unit == "dateTimeAsIso"
                        else []
                    ),
                    "color": {"mode": "palette-classic"},
                    "thresholds": {
                        "mode": "absolute",
                        "steps": thresholds or [{"color": "green", "value": None}],
                    },
                    "custom": {
                        "drawStyle": "line",
                        "lineInterpolation": "linear",
                        "lineWidth": 2,
                        "fillOpacity": 8,
                        "showPoints": "never",
                        "spanNulls": False,
                        "axisBorderShow": False,
                        "axisColorMode": "text",
                    },
                },
                "overrides": [],
            },
            "options": options
            or (
                {
                    "reduceOptions": {"calcs": ["lastNotNull"], "values": False},
                    "textMode": "auto",
                    "colorMode": "value",
                    "graphMode": "area",
                    "justifyMode": "auto",
                }
                if kind == "stat"
                else {
                    "legend": {
                        "displayMode": "table",
                        "placement": "bottom",
                        "calcs": ["lastNotNull"],
                    },
                    "tooltip": {"mode": "multi", "sort": "desc"},
                }
            ),
        }
        if expr:
            panel["datasource"] = P
            panel["targets"] = [
                {
                    "refId": "A",
                    "expr": expr,
                    "legendFormat": "{{channel}} {{backend}} {{operation}} {{outcome}} {{status}} {{component}}",
                    "exemplar": True,
                    "range": True,
                }
            ]
        if kind == "stat":
            panel["options"]["graphMode"] = "none"
            panel["options"]["text"] = {"valueSize": 30, "titleSize": 13}
            panel["options"]["textMode"] = "value"
            panel["options"]["wideLayout"] = False
            panel["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
            panel["fieldConfig"]["defaults"]["thresholds"] = {
                "mode": "absolute",
                "steps": [{"color": "blue", "value": None}],
            }
            if unit == "bool":
                good = title in {
                    "Core ready",
                    "Core available",
                    "Database readable",
                    "Configured",
                    "Usage available",
                    "Total budget verified",
                    "External outage monitor",
                }
                zero = (
                    "Pending"
                    if "verified" in title
                    else "Not reported"
                    if title == "Usage available"
                    else "Not configured"
                    if title in {"Configured", "External outage monitor"}
                    else "Offline"
                    if good
                    else "Off"
                )
                one = (
                    "Verified"
                    if "verified" in title
                    else "Reported"
                    if title == "Usage available"
                    else "Configured"
                    if title in {"Configured", "External outage monitor"}
                    else "Ready"
                    if good
                    else "Paused"
                )
                neutral = title in {
                    "Configured",
                    "Usage available",
                    "Total budget verified",
                    "External outage monitor",
                }
                panel["fieldConfig"]["defaults"]["mappings"] = [
                    {
                        "type": "value",
                        "options": {
                            "0": {
                                "text": zero,
                                "color": "text" if neutral else "red" if good else "green",
                            },
                            "1": {"text": one, "color": "green" if good else "orange"},
                        },
                    }
                ]
            if title in {"Uncertain actions", "Waiting for auth/quota", "CLI failures"}:
                panel["fieldConfig"]["defaults"]["thresholds"]["steps"].append(
                    {"color": "orange", "value": 1}
                )
            for target in panel.get("targets", []):
                target.update(instant=True, range=False)
        if expr:
            labels = []
            grouped = re.search(r"by\s*\(([^)]+)\)", expr)
            if grouped:
                labels = [x.strip() for x in grouped[1].split(",") if x.strip() != "le"]
            elif "__name__" in expr:
                labels = ["__name__"]
            elif "theo_controls" in expr:
                labels = ["control"]
            elif "theo_codex_allowance" in expr:
                labels = ["window"]
            elif "theo_container_" in expr:
                labels = ["component"]
            elif "theo_jobs_current" in expr and "sum(" not in expr:
                labels = ["channel", "status"]
            elif "theo_outbox_current" in expr or "theo_telegram_events_current" in expr:
                labels = ["status"]
            elif "theo_goals_current" in expr:
                labels = ["status"]
            elif "theo_codex_models_current" in expr:
                labels = ["model", "status"]
            elif "theo_codex_runtime_info" in expr:
                labels = ["version", "billing_mode"]
                panel["options"]["textMode"] = "name"
            elif "otel_sdk" in expr:
                labels = ["otel_component_type"]
            elif "otelcol_exporter_queue" in expr:
                labels = ["exporter"]
            elif expr == "up":
                labels = ["instance"]
            for target in panel["targets"]:
                target["legendFormat"] = (
                    " · ".join("{{" + x + "}}" for x in labels) if labels else title
                )
        if kind == "timeseries":
            panel["maxDataPoints"] = 480
            panel["interval"] = "30s"
            panel["fieldConfig"]["defaults"]["min"] = 0
            panel["fieldConfig"]["overrides"] = color_overrides()
            if unit == "short":
                panel["fieldConfig"]["defaults"]["decimals"] = 0
        self.panels.append(panel)
        return panel

    def section(self, title):
        self.add(
            "text",
            title,
            24,
            1,
            options={
                "mode": "html",
                "content": f'<p style="font-size:14px;font-weight:600;color:#b8bdc9;margin:6px 0">{escape(title)}</p>',
            },
        )
        self.y += 1

    def stats(self, values):
        w = 24 // len(values)
        for i, (title, expr, unit) in enumerate(values):
            self.add("stat", title, w, 3, x=i * w, expr=expr, unit=unit)
        self.y += 3

    def charts(self, values):
        w = 24 // len(values)
        for i, (title, expr, unit) in enumerate(values):
            self.add("timeseries", title, w, 8, x=i * w, expr=expr, unit=unit)
        self.y += 8

    def logs(self, query='{service_name=~"theo.*"}'):
        panel = self.add(
            "logs",
            "Recent events · expand a line to open its trace",
            24,
            9,
            options={
                "showTime": True,
                "showLabels": False,
                "showCommonLabels": False,
                "wrapLogMessage": True,
                "sortOrder": "Descending",
                "dedupStrategy": "none",
                "enableLogDetails": True,
            },
        )
        panel["datasource"] = {"type": "loki", "uid": "loki"}
        panel["targets"] = [
            {
                "refId": "A",
                "expr": query,
                "queryType": "range",
                "maxLines": 100,
                "datasource": panel["datasource"],
            }
        ]
        self.y += 9

    def log_charts(self, values):
        self.charts(values)
        for panel in self.panels[-len(values) :]:
            panel["datasource"] = {"type": "loki", "uid": "loki"}
            panel["description"] = (
                "Computed from individually logged CLI observations, including short-lived sessions that cannot supply multiple counter samples. Open the log stream below to inspect correlated traces."
            )
            for target in panel["targets"]:
                target.pop("exemplar", None)
                target["queryType"] = "range"
                target["expr"] = target["expr"].replace("$__rate_interval", "5m")
                target["datasource"] = panel["datasource"]

    def state_bars(self, title, expr, *, x=0, w=12, h=7):
        panel = self.add(
            "bargauge",
            title,
            w,
            h,
            x=x,
            expr=expr,
            options={
                "orientation": "horizontal",
                "displayMode": "basic",
                "showUnfilled": True,
                "reduceOptions": {"calcs": ["lastNotNull"], "values": False},
                "text": {"titleSize": 13, "valueSize": 17},
                "minVizHeight": 24,
                "maxVizHeight": 42,
                "valueMode": "color",
                "namePlacement": "left",
                "sizing": "manual",
            },
        )
        panel["fieldConfig"]["defaults"].update(
            min=0, decimals=0, displayName="${__field.labels.status}"
        )
        panel["fieldConfig"]["overrides"] = color_overrides()
        panel["description"] = (
            "Current durable counts, not a rate. Historical states remain until database retention removes them."
        )
        for target in panel["targets"]:
            target.update(instant=True, range=False)
            target.pop("exemplar", None)
        return panel

    def attention(self):
        self.add(
            "alertlist",
            "Attention now · local host",
            12,
            7,
            options={
                "viewMode": "list",
                "groupMode": "default",
                "maxItems": 5,
                "sortOrder": 3,
                "dashboardAlerts": False,
                "alertName": "Theo",
                "showInstances": False,
                "showInactiveAlerts": False,
                "alertInstanceLabelFilter": '{service="theo"}',
                "stateFilter": {
                    "firing": True,
                    "pending": True,
                    "recovering": True,
                    "error": True,
                    "noData": True,
                    "normal": False,
                },
            },
            description="Current local-host alerts, independent of the traffic and time filters. Open an alert for its recovery guidance.",
        )
        self.state_bars("Work inventory · current", "sum by(status)(theo_jobs_current)", x=12)
        self.y += 7

    def save(self):
        polish(self)
        for panel in self.panels:
            if panel.get("datasource", {}).get("uid") == "prometheus":
                for target in panel.get("targets", []):

                    def scoped(match):
                        metric, selector = match.group(1), match.group(2)
                        selector = selector[1:-1] + "," if selector else ""
                        return metric + "{" + selector + 'environment=~"$environment"}'

                    if "{__name__" in target["expr"]:
                        target["expr"] = re.sub(
                            r"(\{__name__[^}]+)\}",
                            r'\1,environment=~"$environment"}',
                            target["expr"],
                        )
                    else:
                        target["expr"] = re.sub(
                            r"\b((?:theo|otel_sdk)_[a-z_]+)(\{[^}]*\})?", scoped, target["expr"]
                        )
            elif panel.get("datasource", {}).get("uid") == "loki":
                for target in panel["targets"]:
                    target["expr"] = target["expr"].replace(
                        "}", ',deployment_environment_name=~"$environment"}', 1
                    )
        d = {
            "uid": self.uid,
            "title": self.title,
            "description": "Native Theo observability. Provisioned from source; unknown is never healthy.",
            "tags": ["theo", "observability", "local"],
            "timezone": "browser",
            "schemaVersion": 39,
            "version": 1,
            "editable": False,
            "refresh": "1m",
            "time": {"from": "now-24h", "to": "now"},
            "timepicker": {"refresh_intervals": ["1m", "5m", "15m"]},
            "graphTooltip": 1,
            "templating": {
                "list": [
                    {
                        "name": "environment",
                        "label": "Traffic",
                        "type": "custom",
                        "query": "Local Theo : local,Production Theo : production,Test fixtures : qualification",
                        "current": {"text": "Local Theo", "value": "local"},
                        "options": [
                            {"text": "Local Theo", "value": "local", "selected": True},
                            {"text": "Production Theo", "value": "production", "selected": False},
                            {"text": "Test fixtures", "value": "qualification", "selected": False},
                        ],
                    }
                ]
            },
            "links": [
                {
                    "title": label,
                    "url": "/d/" + uid,
                    "type": "link",
                    "keepTime": True,
                    "includeVars": True,
                }
                for uid, label in LINKS
            ]
            + [{"title": "Alerts", "url": "/alerting/list?view=state", "type": "link"}],
            "panels": self.panels,
            "annotations": {
                "list": [
                    {
                        "builtIn": 1,
                        "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                        "enable": True,
                        "hide": True,
                        "iconColor": "rgba(0, 211, 255, 1)",
                        "name": "Annotations & Alerts",
                        "type": "dashboard",
                    }
                ]
            },
        }
        (ROOT / "dashboards" / f"{self.uid}.json").write_text(json.dumps(d, indent=2) + "\n")


def latency(operation, quantile=".95"):
    return f'histogram_quantile({quantile}, sum by(le, operation) (rate(theo_operation_duration_seconds_bucket{{operation=~"{operation}"}}[$__rate_interval])))'


def throughput(operation):
    return f'sum by(operation, outcome) (rate(theo_operation_duration_seconds_count{{operation=~"{operation}"}}[$__rate_interval]))'


def build():
    d = Dashboard(
        "theo-overview",
        "Theo / Operations",
        "Resolve active alerts, then follow blocked work from its events to the full trace.",
    )
    d.stats(
        [
            ("Core ready", "max(theo_core_ready)", "bool"),
            ("Waiting jobs", 'sum(theo_jobs_current{status=~"queued|waiting.*"})' + ZERO, "short"),
            ("Running jobs", 'sum(theo_jobs_current{status="running"})' + ZERO, "short"),
            ("Uncertain actions", "max(theo_actions_uncertain)", "short"),
            ("Stack memory", "max(theo_observability_memory_bytes)", "bytes"),
            (
                "Last completion",
                "max(theo_last_completion_timestamp_seconds)*1000",
                "dateTimeAsIso",
            ),
        ]
    )
    d.attention()
    d.section("Work flowing through Theo")
    d.charts(
        [
            ("Response path · p95", latency("job.run|ai.run|delivery.send"), "s"),
            (
                "Completed and blocked work",
                "sum by(outcome, channel) (increase(theo_jobs_total[$__rate_interval]))",
                "short",
            ),
        ]
    )
    d.stats(
        [
            ("Oldest queued work", "max(theo_queue_oldest_seconds)", "s"),
            ("Autonomy paused", 'max(theo_controls{control="background_paused"})', "bool"),
            ("Awaiting approval", "max(theo_approvals_pending)", "short"),
            ("Outbox delay", "max(theo_outbox_oldest_seconds)", "s"),
        ]
    )
    d.section("Channels & Codex")
    d.charts(
        [
            ("Channel operations", throughput("telegram.*|cli.*"), "ops"),
            (
                "Codex first output · p95",
                'histogram_quantile(0.95,sum by(le,backend)(rate(theo_ai_first_output_duration_seconds_bucket{backend="codex"}[$__rate_interval])))',
                "s",
            ),
        ]
    )
    d.charts(
        [
            (
                "Queue age and Telegram processing lag",
                '{__name__=~"theo_queue_oldest_seconds|theo_telegram_lag_seconds|theo_outbox_oldest_seconds"}',
                "s",
            ),
            ("Memory per component · 4 GB total budget", "theo_container_memory_bytes", "bytes"),
        ]
    )
    d.logs()
    d.save()

    d = Dashboard(
        "theo-jobs",
        "Theo / Jobs & Tools",
        "Separate queued work from blocked work. Inspect the outcome before retrying.",
    )
    d.stats(
        [
            ("Queued", 'sum(theo_jobs_current{status="queued"})' + ZERO, "short"),
            ("Running", 'sum(theo_jobs_current{status="running"})' + ZERO, "short"),
            (
                "Waiting for auth/quota",
                'sum(theo_jobs_current{status=~"waiting_for_auth|waiting_for_quota"})' + ZERO,
                "short",
            ),
            ("Approvals", "max(theo_approvals_pending)", "short"),
        ]
    )
    d.charts(
        [
            ("Work state over time", "sum by(status)(theo_jobs_current)", "short"),
            (
                "Queue latency · p95",
                "histogram_quantile(0.95,sum by(le,channel)(rate(theo_queue_duration_seconds_bucket[$__rate_interval])))",
                "s",
            ),
        ]
    )
    d.charts(
        [
            (
                "Tool outcomes",
                "sum by(tool,outcome)(increase(theo_tools_total[$__rate_interval]))",
                "short",
            ),
            (
                "Tool latency · p95",
                'histogram_quantile(0.95,sum by(le,tool)(rate(theo_operation_duration_seconds_bucket{operation="tool.call"}[$__rate_interval])))',
                "s",
            ),
        ]
    )
    d.charts(
        [
            ("Execution latency", latency("job.run"), "s"),
            ("Durable controls", "theo_controls", "bool"),
        ]
    )
    d.logs('{service_name=~"theo.*"} | event=~"job.*|tool.*|delivery.*"')
    d.charts(
        [
            ("Goals by state", "theo_goals_current", "short"),
            ("Schedule lateness", "theo_schedule_overdue_seconds", "s"),
        ]
    )
    d.save()

    d = Dashboard(
        "theo-telegram",
        "Theo / Telegram",
        "Check polling first, then intake and delivery. Open a delivery event to investigate a missing reply.",
    )
    d.stats(
        [
            ("Configured", 'max(theo_channel_configured{channel="telegram"})', "bool"),
            (
                "Last poll age",
                "time()-max(last_over_time(theo_telegram_poll_success_timestamp[24h]))",
                "s",
            ),
            (
                "Pending updates",
                'sum(theo_telegram_events_current{status="pending"})' + ZERO,
                "short",
            ),
            ("Processing lag", "max(theo_telegram_lag_seconds)", "s"),
        ]
    )
    d.charts(
        [
            (
                "Processing & send latency · p95",
                latency("telegram.process|telegram.receive|telegram.send"),
                "s",
            ),
            ("Polling & message operations", throughput("telegram.*"), "ops"),
        ]
    )
    d.charts(
        [
            ("Persisted update outcomes", "theo_telegram_events_current", "short"),
            ("Delivery queue · all channels", "theo_outbox_current", "short"),
        ]
    )
    d.charts(
        [
            ("Attachment hydration latency", latency("telegram.hydrate"), "s"),
            (
                "Delivery outcomes",
                'sum by(outcome)(increase(theo_deliveries_total{channel="telegram"}[$__rate_interval]))',
                "short",
            ),
        ]
    )
    d.logs('{service_name=~"theo.*"} | event=~"telegram.*|delivery.*"')
    d.charts(
        [
            (
                "Media intake by kind and outcome",
                "sum by(kind,outcome)(increase(theo_telegram_media_total[$__rate_interval]))",
                "short",
            ),
            (
                "Telegram admission to delivery · p95",
                "histogram_quantile(0.95,sum by(le)(rate(theo_telegram_delivery_duration_seconds_bucket[$__rate_interval])))",
                "s",
            ),
        ]
    )
    d.save()

    d = Dashboard(
        "theo-cli",
        "Theo / CLI",
        "Compare submission, first reply and completion. No samples? Select a period with terminal activity.",
    )
    d.stats(
        [
            ("Core available", "max(theo_core_ready)", "bool"),
            ("CLI queued", 'sum(theo_jobs_current{channel="cli",status="queued"})' + ZERO, "short"),
            (
                "CLI running",
                'sum(theo_jobs_current{channel="cli",status="running"})' + ZERO,
                "short",
            ),
            (
                "CLI failures",
                'sum(increase(theo_operation_duration_seconds_count{channel="cli",outcome="error"}[$__range]))',
                "short",
            ),
        ]
    )
    d.log_charts(
        [
            (
                "Connect and submit latency",
                'quantile_over_time(0.95,{service_name=~"theo.*"} | event=~"cli.(connect|submit).finished" | unwrap duration_seconds | __error__="" [$__rate_interval]) by(event)',
                "s",
            ),
            (
                "CLI operations",
                'sum by(event)(rate({service_name=~"theo.*"} | event=~"cli.(connect|submit|cancel).finished" [$__rate_interval]))',
                "ops",
            ),
        ]
    )
    d.charts(
        [
            (
                "Turn latency · core execution",
                'histogram_quantile(0.95,sum by(le)(rate(theo_operation_duration_seconds_bucket{operation="job.run",channel="cli"}[$__rate_interval])))',
                "s",
            ),
            (
                "CLI job outcomes",
                'sum by(outcome)(increase(theo_jobs_total{channel="cli"}[$__rate_interval]))',
                "short",
            ),
        ]
    )
    d.log_charts(
        [
            (
                "First visible response · terminal",
                'quantile_over_time(0.95,{service_name=~"theo.*"} | event="cli.first_visible.measured" | unwrap duration_seconds | __error__="" [$__rate_interval]) by(event)',
                "s",
            ),
            (
                "Completed turn · terminal",
                'quantile_over_time(0.95,{service_name=~"theo.*"} | event="cli.turn_complete.measured" | unwrap duration_seconds | __error__="" [$__rate_interval]) by(event)',
                "s",
            ),
        ]
    )
    d.logs('{service_name=~"theo.*"} | channel="cli"')
    d.save()

    d = Dashboard(
        "theo-codex",
        "Theo / Codex",
        "Codex checks its account and included allowance automatically before each run. Token and allowance charts appear when Codex reports usage.",
    )
    d.stats(
        [
            (
                "Last account check",
                'max(theo_account_verified_timestamp_seconds{backend="codex"})*1000',
                "dateTimeAsIso",
            ),
            ("Usage available", "max(theo_codex_usage_reported)", "bool"),
            (
                "First output · p95",
                'histogram_quantile(0.95,sum by(le)(rate(theo_ai_first_output_duration_seconds_bucket{backend="codex"}[$__rate_interval])))',
                "s",
            ),
            (
                "Primary allowance used",
                'max(theo_codex_allowance_used_ratio{window="primary"})',
                "percentunit",
            ),
        ]
    )
    d.state_bars("Run inventory · last 24 hours", "sum by(status)(theo_codex_models_current)", h=8)
    d.add(
        "timeseries",
        "Attempt duration · p95",
        12,
        8,
        x=12,
        expr=latency("ai.run").replace(
            'operation=~"ai.run"', 'operation=~"ai.run",backend="codex"'
        ),
        unit="s",
        description="All Codex attempts, including authentication and quota waits. First output measures response latency after inference starts.",
    )
    d.y += 8
    d.charts(
        [
            (
                "Reported tokens",
                'sum by(token_type)(increase(theo_ai_tokens_total{backend="codex"}[$__rate_interval]))',
                "short",
            ),
            ("Reported allowance windows", "theo_codex_allowance_used_ratio", "percentunit"),
        ]
    )
    d.stats(
        [
            (
                "Last usage observation",
                "max(theo_codex_usage_observed_timestamp)*1000",
                "dateTimeAsIso",
            ),
            (
                "Primary reset",
                'max(theo_codex_allowance_reset_timestamp{window="primary"})*1000',
                "dateTimeAsIso",
            ),
            ("Cached input · last report", "max(theo_codex_cached_input_tokens)", "short"),
        ]
    )
    d.logs('{service_name=~"theo.*"} | backend="codex"')
    d.charts(
        [
            ("Codex connection · p95", latency("codex.connect"), "s"),
            (
                "Codex run outcomes",
                'sum by(outcome)(increase(theo_ai_runs_total{backend="codex"}[$__rate_interval]))',
                "short",
            ),
        ]
    )
    d.stats(
        [
            (
                "Allowance observation age",
                "time()-max(theo_codex_allowance_observed_timestamp)",
                "s",
            ),
            ("Runtime attestation", "theo_codex_runtime_info", "short"),
        ]
    )
    d.save()

    d = Dashboard(
        "theo-infrastructure",
        "Theo / Infrastructure",
        "Watch the full 4 GB budget, then inspect container pressure and telemetry delivery.",
    )
    d.stats(
        [
            ("Whole observability stack", "max(theo_observability_memory_bytes)", "bytes"),
            (
                "Budget headroom",
                "clamp_min(max(theo_observability_budget_bytes)-max(theo_observability_memory_bytes),0)",
                "decbytes",
            ),
            ("Container restarts", "sum(theo_container_restarts)", "short"),
            ("Total budget verified", "max(theo_observability_budget_verified)", "bool"),
        ]
    )
    d.charts(
        [
            ("Whole stack · 4 GB ceiling", "theo_observability_memory_bytes", "decbytes"),
            (
                "Container memory / hard limit",
                "theo_container_memory_bytes / theo_container_memory_limit_bytes",
                "percentunit",
            ),
        ]
    )
    d.charts(
        [
            ("Container memory", "theo_container_memory_bytes", "bytes"),
            (
                "Native process memory · outside VM",
                '{__name__=~"theo_core_memory_bytes|theo_observer_memory_bytes"}',
                "bytes",
            ),
        ]
    )
    d.charts(
        [
            ("Native Mac CPU", "theo_host_cpu_ratio", "percentunit"),
            ("Mac memory", "theo_host_memory_used_bytes", "bytes"),
        ]
    )
    d.charts(
        [
            ("Scrape targets", "up", "bool"),
            (
                "Telemetry exporter failures",
                "sum by(job,instance)(rate(otelcol_exporter_send_failed_spans_total[$__rate_interval]))",
                "ops",
            ),
        ]
    )
    d.stats(
        [
            ("Disk available", "max(theo_host_disk_free_bytes)", "bytes"),
            ("Database readable", "max(theo_database_readable)", "bool"),
            ("Core uptime", "max(theo_core_uptime_seconds)", "s"),
            ("Collector refresh age", "time()-max(theo_observer_refresh_timestamp_seconds)", "s"),
        ]
    )
    d.logs()
    d.charts(
        [
            (
                "Whole stack / 4 GB budget",
                "theo_observability_memory_bytes / theo_observability_budget_bytes",
                "percentunit",
            ),
            ("Container restarts", "theo_container_restarts", "short"),
        ]
    )
    d.charts(
        [
            (
                "Collector export queue utilization",
                "otelcol_exporter_queue_size / otelcol_exporter_queue_capacity",
                "percentunit",
            ),
            (
                "SDK dropped spans and logs",
                'sum by(error_type)({__name__=~"otel_sdk_processor_(span|log)_processed_total",error_type!=""})',
                "short",
            ),
        ]
    )
    d.charts(
        [
            (
                "SDK buffered spans and logs",
                '{__name__=~"otel_sdk_processor_(span|log)_queue_size"}',
                "short",
            ),
            ("Telemetry freshness", "time()-max(theo_runtime_telemetry_timestamp)", "s"),
        ]
    )
    d.stats(
        [
            ("External outage monitor", "max(theo_external_heartbeat_configured)", "bool"),
            (
                "Last external heartbeat",
                "max(theo_external_heartbeat_sent_timestamp_seconds)*1000",
                "dateTimeAsIso",
            ),
        ]
    )
    d.save()

    env_path = ROOT.parent / ".env"
    telegram_enabled = bool(os.getenv("THEO_ALERT_BOT_TOKEN")) or (
        env_path.exists()
        and any(
            line.startswith("THEO_ALERT_BOT_TOKEN=") and line.split("=", 1)[1]
            for line in env_path.read_text().splitlines()
        )
    )
    build_alerting(ROOT, telegram_enabled)


if __name__ == "__main__":
    build()
