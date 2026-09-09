"""Record and evaluate operator-provided deployment qualification evidence.

Checks native, isolation, behavior and lifecycle reports against explicit gates.
Configuration flags alone cannot attest that a deployment is qualified.
"""

import json
import sys
from typing import cast

from theo.config import Settings
from theo.domain import Denied, Json, encode, uid
from theo.storage import Database


async def qualification_status(db: Database, settings: Settings) -> Json:
    rows = await db.read(
        "SELECT * FROM qualification_results WHERE owner_id=? ORDER BY created_at",
        (settings.owner_id,),
    )
    latest = {(row["kind"], row["backend"]): row for row in rows}

    def passed(kind: str, backend: str | None = None) -> bool:
        row = latest.get((kind, backend))
        return bool(row and row["status"] == "passed" and json.loads(row["evidence"]))

    native = {name: passed("native_canary", name) for name in ("claude", "codex", "cursor", "grok")}
    for name in native:
        if native[name]:
            record = latest[("native_canary", name)]
            account = await db.one(
                "SELECT id FROM backend_accounts WHERE owner_id=? AND backend=? AND fingerprint=? AND status='verified' AND verified_at>?",
                (settings.owner_id, name, record["fingerprint"], db.clock() - 86400),
            )
            native[name] = account is not None
    gates = {
        "claude_live_canary": native["claude"],
        "codex_live_canary": native["codex"],
        "mac_deployment": sys.platform == "darwin"
        and passed("mac_deployment")
        and settings.isolation_verified,
        "encrypted_storage": settings.encrypted_storage_verified,
        "behaviour_30_scenarios": passed("behaviour"),
        "genuine_seven_day_soak": passed("seven_day_soak"),
        "target_capacity_and_restore": passed("capacity_restore"),
        "mandatory_deterministic_suite": passed("deterministic"),
    }
    return {
        "production_qualified": all(gates.values()),
        "gates": gates,
        "native_backends": native,
        "note": "No configuration flag substitutes for elapsed observation or native account canaries.",
    }


async def record_qualification(db: Database, settings: Settings, report: Json) -> str:
    """Operator-only evidence registration; model grants do not expose this function."""
    kind, raw_evidence, backend = report.get("kind"), report.get("evidence"), report.get("backend")
    if not isinstance(raw_evidence, dict):
        raise ValueError("Evidence must be an object")
    evidence = cast(Json, raw_evidence)
    if not evidence.get("source"):
        raise ValueError("Evidence needs an attributable source report and observable measurements")
    fingerprint = None
    if kind == "native_canary":
        if backend not in ("claude", "codex", "cursor", "grok"):
            raise ValueError("Native canary needs a supported backend")
        runs: list[str] = evidence.get("run_ids", [])
        if len(set(runs)) < 5:
            raise Denied("Native qualification requires at least five recorded canary runs")
        for run_id in runs:
            row = await db.one(
                "SELECT * FROM runs WHERE id=? AND owner_id=? AND backend=? AND status='completed'",
                (run_id, settings.owner_id, backend),
            )
            if not row or not row["context_id"]:
                raise Denied("Canary run/context evidence is missing or incomplete")
        account = await db.one(
            "SELECT fingerprint FROM backend_accounts WHERE owner_id=? AND backend=? AND status='verified' AND verified_at>? ORDER BY verified_at DESC LIMIT 1",
            (settings.owner_id, backend, db.clock() - 86400),
        )
        if not account:
            raise Denied(
                "Reverify the included subscription account before recording qualification"
            )
        fingerprint = account["fingerprint"]
        required = (
            "hard_stop",
            "shared_tools",
            "canonical_handoff",
            "cancel_process_tree",
            "media",
            "auth_and_quota_wait",
        )
        if any(evidence.get(check) is not True for check in required):
            raise Denied(
                "Native hard-stop, tools, handoff, cancellation, media and wait canaries must pass"
            )
    elif kind == "mac_deployment":
        import platform

        if (
            sys.platform != "darwin"
            or platform.machine() != "arm64"
            or not settings.isolation_verified
        ):
            raise Denied(
                "Mac deployment evidence must be recorded on the verified Apple Silicon target"
            )
        if any(
            evidence.get(check) is not True
            for check in (
                "protected_paths",
                "service_recovery",
                "maintenance_pause",
                "owner_alert",
                "restore_quarantine",
                "release_rollback",
            )
        ):
            raise Denied("Target deployment checks are incomplete")
    elif kind == "behaviour":
        cases: list[Json] = evidence.get("cases", [])
        if len({case.get("id") for case in cases}) < 30 or any(
            case.get("critical_violation") is not False for case in cases
        ):
            raise Denied("All 30 cases need recorded grades and zero critical violations")
        if sum(case.get("acceptable") is True for case in cases) / len(cases) < 0.9:
            raise Denied("Behaviour evaluation is below the 90% acceptance threshold")
        if any(not case.get("run_id") or not case.get("review_notes") for case in cases):
            raise Denied("Each grade requires run attribution and review notes")
    elif kind == "seven_day_soak":
        start, end = float(evidence.get("started_at", 0)), float(evidence.get("ended_at", 0))
        if end - start < 7 * 86400 or end > db.clock() + 60 or not start:
            raise Denied("A genuine elapsed seven-day interval is required")
        lifecycle = await db.one(
            "SELECT count(*) n FROM lifecycle_intervals WHERE owner_id=? AND started_at<=? AND heartbeat_at>=?",
            (settings.owner_id, end, start),
        )
        if (
            not lifecycle
            or not lifecycle["n"]
            or evidence.get("availability", 0) < 0.995
            or not evidence.get("awake_intervals")
        ):
            raise Denied(
                "Soak needs lifecycle, declared awake intervals and measured 99.5% availability"
            )
        if evidence.get("critical_violations") != 0:
            raise Denied("Soak has unresolved critical violations")
    elif kind == "capacity_restore":
        if (
            evidence.get("memories", 0) < 20000
            or evidence.get("messages", 0) < 250000
            or evidence.get("context_p95_ms", 9999) >= 750
            or evidence.get("restore_seconds", 9999) >= 1800
            or evidence.get("warm_local_embeddings") is not True
        ):
            raise Denied("Full target capacity/restore thresholds are not met")
    elif kind == "deterministic":
        if (
            set(evidence.get("acceptance_ids", [])) != {f"A{i:02}" for i in range(1, 41)}
            or evidence.get("failed") != 0
            or evidence.get("skipped") != 0
        ):
            raise Denied("Every mandatory acceptance case must pass without skips")
    else:
        raise ValueError("Unknown qualification kind")
    record = uid()
    await db.execute(
        "INSERT INTO qualification_results VALUES(?,?,?,?,?,?,?,?)",
        (
            record,
            settings.owner_id,
            kind,
            backend,
            fingerprint,
            "passed",
            encode(evidence),
            db.clock(),
        ),
    )
    return record
