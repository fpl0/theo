"""Verify native account eligibility and construct worker environments.

Matches backend, model, runtime and configuration evidence, tracks shared quota
pools and rejects unverified or metered routes before inference starts.
"""

import json
import os
import pwd
import re
from pathlib import Path

from theo.domain import AuthWait, Denied, Json, QuotaWait, digest, encode, uid
from theo.storage import Database

BACKENDS = ("claude", "codex", "cursor", "grok")
FORBIDDEN_ENV = re.compile(
    r"(API_KEY|AUTH_TOKEN|ACCESS_TOKEN|BASE_URL|ENDPOINT|BEDROCK|VERTEX|FOUNDRY|AZURE|AWS_|GOOGLE_APPLICATION_CREDENTIALS|CLOUD_ML|EXTRA_USAGE|TOP_UP|NODE_OPTIONS|PYTHONPATH|LD_PRELOAD|DYLD_)",
    re.I,
)
FORBIDDEN_CONFIG = re.compile(
    r"(api.?key|api.?key.?helper|base.?url|endpoint|bedrock|vertex|foundry|extra.?usage|auto.?top.?up|on.?demand|fast.?mode|model.?provider|credential.?process)",
    re.I,
)
SAFE_ENV = {"PATH", "LANG", "LC_ALL", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR", "SYSTEMROOT"}


def inspect_environment(environment: dict[str, str]) -> list[str]:
    return sorted(key for key, value in environment.items() if value and FORBIDDEN_ENV.search(key))


def worker_environment(
    home: Path, environment: dict[str, str] | None = None, *, runner_uid: int | None = None
) -> dict[str, str]:
    source = dict(os.environ) if environment is None else environment
    contaminated = inspect_environment(source)
    if contaminated:
        raise Denied("Disallowed worker environment keys: " + ", ".join(contaminated))
    clean = {key: value for key, value in source.items() if key in SAFE_ENV}
    username = pwd.getpwuid(os.geteuid() if runner_uid is None else runner_uid).pw_name
    clean.update(
        {
            "HOME": str(home),
            "USER": username,
            "LOGNAME": username,
            "TMPDIR": str(home / "tmp"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "DO_NOT_TRACK": "1",
        }
    )
    return clean


def inspect_configuration(files: list[Path]) -> str:
    fingerprints: list[Json] = []
    for path in files:
        if not path.exists():
            fingerprints.append({"path": str(path), "missing": True})
            continue
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise Denied("Unsafe provider configuration file")
        text = path.read_text()
        # Fail closed on ambiguous configuration, retaining hashes, never its secret content.
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if FORBIDDEN_CONFIG.search(line) and not re.search(
                r"[:=]\s*(false|null|\"openai\"|\"\")\s*[,}]?\s*$", line, re.I
            ):
                raise Denied("Provider configuration contains a paid or custom route control")
        fingerprints.append({"path": str(path), "hash": digest(text)})
    return digest(fingerprints)


def configuration_files(home: Path, backend: str) -> list[Path]:
    return {
        "claude": [home / ".claude/settings.json", home / ".claude.json"],
        "codex": [home / ".codex/config.toml"],
        "cursor": [home / ".cursor/cli-config.json"],
        "grok": [home / ".grok/settings.json", home / ".grok/config.toml"],
    }[backend]


class Accounts:
    def __init__(self, db: Database, owner: str):
        self.db, self.owner = db, owner

    async def register(self, backend: str, evidence: Json) -> str:
        required = {
            "account_ref",
            "label",
            "pool_id",
            "models",
            "runtime_version",
            "fingerprint",
            "config_hash",
            "verification_method",
            "native_subscription_login",
            "extra_usage_disabled",
            "hard_stop_verified",
            "evidence",
        }
        if backend not in BACKENDS or set(evidence) != required:
            raise ValueError("Account evidence fields do not match the verification schema")
        if not all(
            evidence[k] is True
            for k in ("native_subscription_login", "extra_usage_disabled", "hard_stop_verified")
        ):
            raise Denied("Native subscription login and hard spending stop must be verified")
        if (
            not evidence["models"]
            or not evidence["evidence"]
            or evidence["verification_method"]
            not in ("native_and_operator_attestation", "native_machine_verified")
        ):
            raise Denied("Missing account controls/catalogue evidence")
        account_id = uid()
        await self.db.execute(
            "INSERT INTO backend_accounts(id,owner_id,backend,account_ref,label,billing_mode,pool_id,models,capabilities,fingerprint,runtime_version,config_hash,verified_at,method,evidence,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,backend,account_ref) DO UPDATE SET pool_id=excluded.pool_id,models=excluded.models,fingerprint=excluded.fingerprint,runtime_version=excluded.runtime_version,config_hash=excluded.config_hash,verified_at=excluded.verified_at,method=excluded.method,evidence=excluded.evidence,status='verified'",
            (
                account_id,
                self.owner,
                backend,
                evidence["account_ref"],
                evidence["label"],
                "included_subscription",
                evidence["pool_id"],
                encode(evidence["models"]),
                encode(["text", "tools"]),
                evidence["fingerprint"],
                evidence["runtime_version"],
                evidence["config_hash"],
                self.db.clock(),
                evidence["verification_method"],
                encode(evidence),
                "verified",
            ),
        )
        row = await self.db.one(
            "SELECT id FROM backend_accounts WHERE owner_id=? AND backend=? AND account_ref=?",
            (self.owner, backend, evidence["account_ref"]),
        )
        assert row
        return str(row["id"])

    async def eligible(self, backend: str, model: str, fingerprint: str, config_hash: str) -> Json:
        rows = await self.db.read(
            "SELECT * FROM backend_accounts WHERE owner_id=? AND backend=? ORDER BY verified_at DESC",
            (self.owner, backend),
        )
        quota = False
        for account in rows:
            if (
                account["billing_mode"] != "included_subscription"
                or account["status"] != "verified"
            ):
                continue
            if (
                self.db.clock() - account["verified_at"] > 86400
                or account["fingerprint"] != fingerprint
                or account["config_hash"] != config_hash
            ):
                continue
            if model not in json.loads(account["models"]):
                continue
            if account["quota_status"] == "exhausted":
                if account["reset_at"] is None or account["reset_at"] > self.db.clock():
                    quota = True
                    continue
            return account
        if quota:
            raise QuotaWait("Included allowance exhausted; job is preserved")
        raise AuthWait("No current verified included-subscription account for this runtime/model")

    async def exhaust(self, account: Json, reset_at: float | None = None) -> None:
        await self.db.execute(
            "UPDATE backend_accounts SET quota_status='exhausted',reset_at=? WHERE owner_id=? AND pool_id=?",
            (reset_at, self.owner, account["pool_id"]),
        )

    async def usage(self) -> Json:
        accounts = await self.db.read(
            "SELECT backend,label,pool_id,billing_mode,status,quota_status,reset_at,verified_at FROM backend_accounts WHERE owner_id=?",
            (self.owner,),
        )
        metrics = await self.db.one(
            "SELECT count(*) runs,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,sum(billed_minor_units) billed_minor_units,sum(CASE WHEN input_tokens IS NULL THEN 1 ELSE 0 END) unknown_usage_runs FROM usage_observations WHERE owner_id=?",
            (self.owner,),
        )
        return {
            "accounts": accounts,
            "observations": metrics,
            "remaining_allowance": None,
            "note": "Unknown is not zero. Token estimates are not invoices; shared pools are not independent quotas.",
        }
