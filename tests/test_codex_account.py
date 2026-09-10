"""Regression checks for fresh native subscription admission and mid-turn stops."""

import copy
import json

import pytest

from theo.backends.codex_account import METHOD, CodexAccount, allowance_snapshot, check_allowance
from theo.backends.policy import Accounts
from theo.domain import AuthWait, QuotaWait


def snapshot(now):
    return {
        "limitId": "codex",
        "primary": {"usedPercent": 20, "resetsAt": now + 3600},
        "secondary": None,
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
    }


class NativeFixture:
    def __init__(self, clock):
        self.clock = clock
        self.account = {"type": "chatgpt", "planType": "pro", "email": "fixture@example.invalid"}
        self.account_id = "synthetic-account"
        self.model = "fixture-model"
        self.allowance = None
        self.calls = []

    async def call(self, method, params):
        self.calls.append(method)
        if method == "account/read":
            assert params["refreshToken"] is True
            return {"account": self.account}
        if method == "model/list":
            return {"data": [{"id": self.model}], "nextCursor": None}
        assert method == "account/rateLimits/read"
        return {
            "accountId": self.account_id,
            "rateLimits": self.allowance or snapshot(self.clock()),
        }


def gate(db):
    return CodexAccount(
        db,
        "owner",
        "fixture-model",
        {
            "fingerprint": "fixture-fingerprint",
            "config_hash": "fixture-config",
            "runtime_version": "fixture-1",
        },
    )


async def test_empty_or_days_old_registry_is_renewed_from_native_observations(db, clock):
    rpc = NativeFixture(clock)
    first = await gate(db).verify(rpc, catalogue=True)
    clock.advance(3 * 86400)
    second = await gate(db).verify(rpc, catalogue=True)
    assert first["id"] == second["id"]
    assert second["verified_at"] == clock()
    assert second["method"] == METHOD
    evidence = json.loads(second["evidence"])
    assert "extra_usage_disabled" not in evidence
    assert "hard_stop_verified" not in evidence
    assert "fixture@example.invalid" not in second["evidence"]
    assert rpc.calls.count("account/rateLimits/read") == 2
    # A saved native observation cannot substitute for the live admission path.
    with pytest.raises(AuthWait):
        await Accounts(db, "owner").eligible(
            "codex", "fixture-model", "fixture-fingerprint", "fixture-config"
        )


@pytest.mark.parametrize(
    "account",
    [
        None,
        {"type": "apiKey"},
        {"type": "chatgpt", "planType": "business"},
        {"type": "chatgpt", "planType": "pro"},
    ],
)
async def test_unknown_or_metered_identity_never_creates_a_verified_record(db, clock, account):
    rpc = NativeFixture(clock)
    rpc.account = account
    with pytest.raises(AuthWait):
        await gate(db).verify(rpc, catalogue=True)
    assert not await db.read("SELECT id FROM backend_accounts")
    assert "model/list" not in rpc.calls


async def test_unavailable_model_does_not_enroll_account(db, clock):
    rpc = NativeFixture(clock)
    rpc.model = "another-model"
    with pytest.raises(AuthWait):
        await gate(db).verify(rpc, catalogue=True)
    assert not await db.read("SELECT id FROM backend_accounts")


@pytest.mark.parametrize(
    "change",
    [
        {"primary": None},
        {"primary": {"usedPercent": None, "resetsAt": 100}},
        {"primary": {"usedPercent": -1, "resetsAt": 100}},
        {"primary": {"usedPercent": float("nan"), "resetsAt": 100}},
        {"primary": {"usedPercent": False, "resetsAt": 100}},
        {"primary": {"usedPercent": 20, "resetsAt": 1}},
        {"credits": None},
        {"credits": {}},
        {"credits": {"hasCredits": False, "unlimited": False, "balance": "NaN"}},
        {"credits": {"hasCredits": True, "unlimited": False, "balance": "10"}},
        {"credits": {"hasCredits": False, "unlimited": True, "balance": "0"}},
    ],
)
def test_unknown_stale_or_paid_allowance_fails_closed(change):
    with pytest.raises(AuthWait):
        check_allowance({**snapshot(10), **change}, 10)


@pytest.mark.parametrize(
    "change",
    [
        {"primary": {"usedPercent": 100, "resetsAt": 100}},
        {"secondary": {"usedPercent": 100, "resetsAt": 100}},
        {"spendControlReached": True},
        {"rateLimitReachedType": "usageLimit"},
    ],
)
def test_any_exhausted_window_or_provider_limit_stops_inference(change):
    with pytest.raises(QuotaWait):
        check_allowance({**snapshot(10), **change}, 10)


def test_general_pool_never_falls_back_to_an_unrelated_available_bucket():
    result = {"rateLimits": snapshot(10), "rateLimitsByLimitId": {"another": snapshot(10)}}
    with pytest.raises(AuthWait):
        allowance_snapshot(result)
    result["rateLimitsByLimitId"]["codex"] = snapshot(10)
    assert allowance_snapshot(result)["limitId"] == "codex"


async def test_account_switch_and_mid_turn_paid_credit_or_quota_updates_stop(db, clock):
    rpc = NativeFixture(clock)
    guard = gate(db)
    await guard.verify(rpc, catalogue=True)
    previous = copy.deepcopy(guard.snapshot)
    guard.update({"limitId": "another", "primary": {"usedPercent": 100}})
    assert guard.snapshot == previous
    with pytest.raises(QuotaWait):
        guard.update(
            {"limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": clock() + 100}}
        )
    await guard.verify(rpc)
    with pytest.raises(AuthWait):
        guard.update(
            {
                "limitId": "codex",
                "credits": {"hasCredits": True, "unlimited": False, "balance": "10"},
            }
        )
    rpc.account_id = "different-workspace"
    with pytest.raises(AuthWait, match="account changed"):
        await guard.verify(rpc)


async def test_native_quota_recovery_keeps_explicit_operator_retry(db, clock):
    rpc = NativeFixture(clock)
    guard = gate(db)
    account = await guard.verify(rpc, catalogue=True)
    await Accounts(db, "owner").exhaust(account)
    with pytest.raises(QuotaWait):
        await gate(db).verify(rpc, catalogue=True)
