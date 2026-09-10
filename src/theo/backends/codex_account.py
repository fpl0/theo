"""Check the active Codex subscription before inference and while a turn runs.

Fresh native observations replace manual daily attestations for this adapter.
They establish current included allowance, not provider billing-setting changes.
Paid credit balances, unknown allowance and metered authentication fail closed.
"""

import math
from decimal import Decimal, InvalidOperation
from typing import cast

from theo.backends.process import RpcProcess
from theo.domain import AuthWait, Json, QuotaWait, digest, encode, uid
from theo.storage import Database

METHOD = "native_live_allowance"


def allowance_snapshot(result: Json) -> Json:
    """Select the general Codex bucket, never an unrelated model's allowance."""
    buckets = result.get("rateLimitsByLimitId")
    if isinstance(buckets, dict):
        snapshot = cast(Json, buckets).get("codex")
    else:
        snapshot = result.get("rateLimits")
    if not isinstance(snapshot, dict):
        raise AuthWait("Codex included allowance is unavailable; your job is preserved")
    snapshot = cast(Json, snapshot)
    if snapshot.get("limitId") not in (None, "codex"):
        raise AuthWait("Codex included allowance is unavailable; your job is preserved")
    return snapshot


def check_allowance(snapshot: Json, now: float) -> None:
    """Require positive, unexpired subscription allowance with no paid credit route."""
    windows = [snapshot.get(name) for name in ("primary", "secondary")]
    observed = False
    for raw in windows:
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise AuthWait("Codex included allowance could not be verified")
        window = cast(Json, raw)
        used, reset = window.get("usedPercent"), window.get("resetsAt")
        if (
            not isinstance(used, (int, float))
            or isinstance(used, bool)
            or not math.isfinite(used)
            or used < 0
            or not isinstance(reset, (int, float))
            or isinstance(reset, bool)
            or not math.isfinite(reset)
            or reset <= now
        ):
            raise AuthWait("Codex included allowance is stale or unknown; your job is preserved")
        observed = True
        if used >= 100:
            raise QuotaWait("Codex included allowance is exhausted")
    if not observed:
        raise AuthWait("Codex included allowance is unknown; your job is preserved")
    if snapshot.get("spendControlReached") is True or snapshot.get("rateLimitReachedType"):
        raise QuotaWait("Codex reports an account usage limit")
    credits = snapshot.get("credits")
    if not isinstance(credits, dict):
        raise AuthWait("Codex credit usage could not be checked; your job is preserved")
    credits = cast(Json, credits)
    try:
        balance = Decimal(str(credits.get("balance")))
    except InvalidOperation:
        raise AuthWait("Codex credit usage could not be checked") from None
    if (
        credits.get("hasCredits") is not False
        or credits.get("unlimited") is not False
        or not balance.is_finite()
        or balance != 0
    ):
        raise AuthWait("Codex reports paid or unknown credits; Theo requires included allowance")


class CodexAccount:
    """Bind account observations to this isolated native process and selected model."""

    def __init__(self, db: Database, owner: str, model: str, runtime: Json):
        self.db, self.owner, self.model, self.runtime = db, owner, model, runtime
        self.identity: str | None = None
        self.account: Json | None = None
        self.snapshot: Json = {}

    async def verify(self, rpc: RpcProcess, *, catalogue: bool = False) -> Json:
        response = await rpc.call("account/read", {"refreshToken": True})
        identity: Json = response.get("account") or {}
        if identity.get("type") != "chatgpt" or identity.get("planType") not in ("plus", "pro"):
            raise AuthWait("Sign in to Codex with a supported ChatGPT subscription")
        email = identity.get("email")
        if not isinstance(email, str) or not email:
            raise AuthWait("Codex subscription identity is unavailable")
        if catalogue:
            # Spark uses a separate quota pool, not the general Codex allowance.
            if "spark" in self.model.lower():
                raise AuthWait("Codex Spark needs a separately verified allowance pool")
            cursor = None
            for _ in range(20):
                models = await rpc.call("model/list", {"cursor": cursor, "limit": 100})
                if any(
                    item.get("id") == self.model or item.get("model") == self.model
                    for item in models.get("data", [])
                ):
                    break
                cursor = models.get("nextCursor")
                if not cursor:
                    raise AuthWait("The selected model is unavailable to this Codex subscription")
            else:
                raise AuthWait("Codex model catalogue exceeded the inspection limit")
        limits = await rpc.call("account/rateLimits/read", {})
        identity_hash = digest(
            {"email": email, "plan": identity["planType"], "account": limits.get("accountId")}
        )
        if self.identity is not None and self.identity != identity_hash:
            raise AuthWait("Codex account changed during this run; your job is preserved")
        snapshot = allowance_snapshot(limits)
        check_allowance(snapshot, self.db.clock())
        self.identity, self.snapshot = identity_hash, snapshot
        account_ref = "codex:" + digest(
            {"identity": identity_hash, "account": limits.get("accountId")}
        )
        pool_id = account_ref + ":codex"
        evidence: Json = {
            "source": "Codex App Server account/read, model/list and account/rateLimits/read",
            "identity_hash": identity_hash,
            "plan": identity["planType"],
            "allowance": snapshot,
            "policy": "Fresh included allowance and zero available paid credits; rechecked every turn and during inference. No claim about provider purchase settings.",
        }
        await self.db.execute(
            "INSERT INTO backend_accounts(id,owner_id,backend,account_ref,label,billing_mode,pool_id,models,capabilities,fingerprint,runtime_version,config_hash,verified_at,method,evidence,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,backend,account_ref) DO UPDATE SET models=excluded.models,fingerprint=excluded.fingerprint,runtime_version=excluded.runtime_version,config_hash=excluded.config_hash,verified_at=excluded.verified_at,method=excluded.method,evidence=excluded.evidence,status='verified'",
            (
                uid(),
                self.owner,
                "codex",
                account_ref,
                "Native Codex subscription",
                "included_subscription",
                pool_id,
                encode([self.model]),
                encode(["text", "tools"]),
                self.runtime["fingerprint"],
                self.runtime["runtime_version"],
                self.runtime["config_hash"],
                self.db.clock(),
                METHOD,
                encode(evidence),
                "verified",
            ),
        )
        account = await self.db.one(
            "SELECT * FROM backend_accounts WHERE owner_id=? AND backend='codex' AND account_ref=?",
            (self.owner, account_ref),
        )
        assert account is not None
        self.account = account
        if account["quota_status"] == "exhausted":
            raise QuotaWait("Confirm quota recovery and explicitly retry the waiting job")
        return account

    def update(self, snapshot: Json) -> None:
        if not self.snapshot or snapshot.get("limitId") not in (None, "codex"):
            return
        self.snapshot = {**self.snapshot, **snapshot}
        check_allowance(self.snapshot, self.db.clock())
