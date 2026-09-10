"""Controller durability and fencing without GitHub credentials or a live daemon."""

import asyncio
import os

import pytest

from theo.domain import Conflict, Denied
from theo.maintenance.contracts import CandidateIdentity, MaintenanceRequest
from theo.maintenance.journal import Journal
from theo.maintenance.policy import MaintenancePolicy, load_policy


@pytest.fixture
def policy():
    return MaintenancePolicy(
        revision=1,
        owner_id="owner",
        installation_id="fixture",
        repository_id=123,
        repository="fixture/theo",
        enabled=True,
    )


@pytest.fixture
def request_data():
    return MaintenanceRequest(
        request_id="request-1",
        owner_id="owner",
        installation_id="fixture",
        job_id="parent",
        coding_job_id="coding",
        conversation_id="conversation",
        origin="requested",
        objective="Repair the fixture behavior",
        target="deploy",
    )


@pytest.fixture
async def journal(tmp_path, clock):
    instance = Journal(tmp_path / "controller", clock)
    await instance.initialize()
    yield instance
    await instance.close()


async def test_accept_once_after_lost_ack_and_database_reopen(
    tmp_path, clock, policy, request_data
):
    first = Journal(tmp_path / "controller", clock)
    await first.initialize()
    accepted = await first.accept(policy, request_data)
    await first.close()
    recovered = Journal(tmp_path / "controller", clock)
    try:
        await recovered.initialize()
        assert (await recovered.accept(policy, request_data))["id"] == accepted["id"]
        assert len(await recovered.read("SELECT * FROM changes")) == 1
        assert len(await recovered.events()) == 1
        assert not await recovered.one("SELECT 1 FROM sqlite_master WHERE name='messages'")
        with pytest.raises(Conflict, match="different"):
            await recovered.accept(
                policy, request_data.model_copy(update={"objective": "Another change"})
            )
    finally:
        await recovered.close()


async def test_concurrent_controller_claims_fence_expired_owner(
    journal, policy, request_data, clock
):
    change = await journal.accept(policy, request_data)
    claims = await asyncio.gather(journal.claim("first"), journal.claim("second"))
    assert sum(lease is not None for lease in claims) == 1
    old = next(lease for lease in claims if lease is not None)
    assert old.change_id == change["id"]
    clock.advance(61)
    replacement = await journal.claim("replacement")
    assert replacement.generation == old.generation + 1
    with pytest.raises(Denied, match="stale"):
        await journal.transition(old, "editing")
    await journal.transition(replacement, "editing", status="waiting")
    state = await journal.one("SELECT * FROM changes")
    assert state["stage"] == "editing" and state["status"] == "waiting"
    assert await journal.claim("third") is None


async def test_cancel_revokes_active_stage_before_its_next_effect(journal, policy, request_data):
    change = await journal.accept(policy, request_data)
    lease = await journal.claim("worker")
    cancelled = await journal.request_cancel(change["id"], lease.revision)
    assert cancelled["cancel_requested"] == 1
    with pytest.raises(Denied):
        await journal.transition(lease, "editing")
    cleanup = await journal.claim("recovery")
    result = await journal.transition(cleanup, "cancelled")
    assert result["status"] == "terminal" and result["stage"] == "cancelled"
    assert await journal.claim("worker") is None


async def test_candidate_revision_is_immutable_and_required_for_verification(
    journal, policy, request_data
):
    change = await journal.accept(policy, request_data)
    await journal.transition(await journal.claim("worker"), "editing")
    lease = await journal.claim("worker")
    with pytest.raises(Denied, match="candidate"):
        await journal.transition(lease, "verifying")
    identity = CandidateIdentity(
        change_id=change["id"],
        revision=1,
        base_commit="1" * 40,
        commit="2" * 40,
        tree="3" * 40,
        snapshot_sha256="4" * 64,
        lock_sha256="5" * 64,
    )
    await journal.record_candidate(lease, identity)
    await journal.record_candidate(lease, identity)
    with pytest.raises(Conflict, match="immutable"):
        await journal.record_candidate(lease, identity.model_copy(update={"commit": "6" * 40}))
    with pytest.raises(Conflict, match="revision"):
        await journal.record_candidate(lease, identity.model_copy(update={"revision": 3}))
    await journal.transition(lease, "verifying")
    assert len(await journal.read("SELECT * FROM candidates")) == 1
    assert (await journal.one("SELECT stage FROM changes"))["stage"] == "verifying"


async def test_unobserved_stages_cannot_be_skipped_and_events_replay_in_order(
    journal, policy, request_data
):
    await journal.accept(policy, request_data)
    lease = await journal.claim("worker")
    with pytest.raises(Conflict, match="transition"):
        await journal.transition(lease, "deployed")
    await journal.transition(lease, "editing", status="waiting")
    first_page = await journal.events(limit=2)
    second_page = await journal.events(after=first_page[-1]["sequence"])
    assert [row["kind"] for row in first_page + second_page] == ["accepted", "claimed", "editing"]
    assert len({row["revision"] for row in first_page + second_page}) == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", "someone-else"),
        ("installation_id", "another-host"),
        ("origin", "autonomous"),
        ("origin", "system"),
    ],
)
async def test_outside_authority_never_creates_a_change(
    journal, policy, request_data, field, value
):
    with pytest.raises(Denied):
        await journal.accept(policy, request_data.model_copy(update={field: value}))
    assert not await journal.read("SELECT * FROM changes")


async def test_current_policy_revocation_prevents_new_requests(journal, policy, request_data):
    revoked = policy.model_copy(update={"enabled": False, "revision": 2})
    with pytest.raises(Denied, match="not enabled"):
        await journal.accept(revoked, request_data)
    assert not await journal.events()


def test_policy_file_rejects_unprotected_owner_and_symlinks(tmp_path, policy):
    path = tmp_path / "policy.json"
    path.write_text(policy.model_dump_json())
    path.chmod(0o600)
    assert load_policy(path, expected_uid=os.getuid()) == policy
    with pytest.raises(Denied):
        load_policy(path, expected_uid=os.getuid() + 1)
    path.chmod(0o666)
    with pytest.raises(Denied):
        load_policy(path)
    path.chmod(0o600)
    link = tmp_path / "policy-link.json"
    link.symlink_to(path)
    with pytest.raises(Denied):
        load_policy(link)


async def editing_candidate(journal, change_id, revision):
    await journal.wake_waiting()
    preparation = await journal.claim("controller")
    await journal.bind_base(preparation, revision, "1" * 40)
    await journal.transition(preparation, "editing")
    editing = await journal.claim("controller")
    candidate = CandidateIdentity(
        change_id=change_id,
        revision=revision,
        base_commit="1" * 40,
        commit=str(revision + 1) * 40,
        tree=str(revision + 2) * 40,
        snapshot_sha256="4" * 64,
        lock_sha256="5" * 64,
    )
    await journal.record_candidate(editing, candidate)
    await journal.transition(editing, "verifying")
    return candidate, await journal.claim("controller")


@pytest.mark.parametrize("origin", ["requested", "autonomous"])
async def test_repair_limits_only_proactive_work_and_preserves_previous_rounds(
    journal, policy, request_data, origin
):
    policy = policy.model_copy(update={"allow_proactive": True})
    request_data = request_data.model_copy(
        update={"origin": origin, "evidence": ("health:fixture",)}
    )
    accepted = await journal.accept(policy, request_data)
    candidate, lease = await editing_candidate(journal, accepted["id"], 1)
    submission = {"revision": 1, "snapshot_sha256": candidate.snapshot_sha256, "summary": "first"}
    await journal.signal(accepted["id"], "submission", submission)
    await journal.revise(lease, candidate, "1" * 40, "review requested repair", policy)
    with pytest.raises(Denied, match="stale"):
        await journal.effect_intent(lease, "push", {})
    assignment = {"revision": 2, "coding_job_id": "code-2", "review_job_id": "review-2"}
    await journal.signal(accepted["id"], "round_jobs", assignment)
    assert await journal.signal(accepted["id"], "round_jobs", assignment) == {"accepted": True}
    assert await journal.signal(accepted["id"], "submission", submission) == {"accepted": True}
    with pytest.raises(Conflict):
        await journal.signal(accepted["id"], "submission", {**submission, "summary": "overwrite"})
    with pytest.raises(Conflict, match="unissued"):
        await journal.signal(accepted["id"], "round_jobs", {**assignment, "revision": 3})
    candidate2, lease2 = await editing_candidate(journal, accepted["id"], 2)
    if origin == "autonomous":
        with pytest.raises(Denied, match="Proactive candidate revision limit"):
            await journal.revise(lease2, candidate2, "1" * 40, "another repair", policy)
        assert (await journal.current_round(accepted["id"]))["revision"] == 2
    else:
        await journal.revise(lease2, candidate2, "1" * 40, "another repair", policy)
        assert (await journal.current_round(accepted["id"]))["revision"] == 3
    assert len(await journal.read("SELECT * FROM candidates")) == 2
    assert await journal.get_signal(accepted["id"], "submission") == submission
    assert await journal.get_signal(accepted["id"], "round-2:submission") is None


async def test_unknown_merge_cannot_be_revised_until_confirmed_no_effect(
    journal, policy, request_data
):
    accepted = await journal.accept(policy, request_data)
    candidate, verifying = await editing_candidate(journal, accepted["id"], 1)
    await journal.transition(verifying, "publishing")
    await journal.transition(await journal.claim("worker"), "awaiting_ci")
    await journal.transition(await journal.claim("worker"), "merging")
    merging = await journal.claim("worker")
    await journal.effect_intent(merging, "merge", {"sha": candidate.commit})
    await journal.effect_receipt(merging, "merge", None)
    with pytest.raises(Conflict, match="Resolve the previous merge"):
        await journal.revise(merging, candidate, "9" * 40, "base advanced", policy)
    assert (await journal.current_round(accepted["id"]))["revision"] == 1
    await journal.no_effect(merging, "merge")
    await journal.revise(merging, candidate, "9" * 40, "base advanced", policy)
    assert (await journal.current_round(accepted["id"]))["base_commit"] == "9" * 40
