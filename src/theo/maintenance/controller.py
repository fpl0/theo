"""Durable source-to-GitHub-to-release orchestration outside the running core.

Every stage observes real service receipts. Replayed remote effects reconcile
before sending; host recovery remains available after policy revocation.
"""

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import shutil
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path

from theo.domain import Conflict, Denied, Json, digest, encode, uid
from theo.execution.processes import terminate_tree
from theo.maintenance.configuration import ControllerConfig, read_operator_file
from theo.maintenance.contracts import (
    CandidateIdentity,
    ControllerLease,
    MaintenanceRequest,
    round_effect,
)
from theo.maintenance.dependencies import stage as stage_dependencies
from theo.maintenance.github import BaseAdvanced, CheckFailed, GitHub, NoEffect, Uncertain
from theo.maintenance.journal import Journal
from theo.maintenance.policy import load_policy
from theo.maintenance.rpc import Client, serve
from theo.maintenance.source import Source, git, read_source, write_source
from theo.maintenance.verification import VerificationFailed, Verifier
from theo.maintenance.workspace_access import handoff


class Controller:
    def __init__(self, config: ControllerConfig, journal: Journal):
        self.config, self.journal = config, journal
        self.source = Source(config.root, config.workspaces)
        self.verifier = Verifier(config)
        self.host = Client(config.host_socket, config.host_token_file)
        self.github = GitHub(config, load_policy(config.policy))
        self.worker = uid()

    def policy(self):
        return load_policy(self.config.policy, expected_uid=self.config.policy_uid)

    async def rpc(self, operation: str, body: Json) -> Json:
        if operation == "accept":
            request = MaintenanceRequest.model_validate(body)
            prior = await self.journal.one(
                "SELECT * FROM changes WHERE request_id=?", (request.request_id,)
            )
            if prior:
                if prior["request_hash"] != digest(request.model_dump(mode="json")):
                    raise Conflict("Request identity already binds another operation")
                return {"id": prior["id"], "stage": prior["stage"]}
            pending = await self.journal.one(
                "SELECT count(*) n FROM changes WHERE status<>'terminal'"
            )
            if pending and pending["n"] >= self.config.max_pending:
                raise Denied("Maintenance pending-work limit reached")
            accepted = await self.journal.accept(self.policy(), request)
            return {"id": accepted["id"], "stage": accepted["stage"]}
        if operation == "events" and set(body) == {"after"}:
            return {"events": await self.journal.events(body["after"])}
        if operation == "signal" and set(body) == {"change_id", "name", "body"}:
            return await self.journal.signal(body["change_id"], body["name"], body["body"])
        raise Denied("Unsupported controller operation")

    async def effect(
        self,
        lease: ControllerLease,
        name: str,
        request: Json,
        perform: Callable[[], Awaitable[Json]],
        reconcile: Callable[[], Awaitable[Json | None]] | None = None,
    ) -> Json:
        previous = await self.journal.effect_intent(lease, name, request)
        if previous["status"] == "succeeded":
            return json.loads(previous["receipt"])
        if previous["status"] != "new" and reconcile:
            result = await reconcile()
            if result is None:
                raise Uncertain("Remote effect has no receipt: " + name)
        else:
            # A stage can contain several awaited effects. Recheck authority for
            # each mutation, including a revocation that arrived during the last one.
            row = await self.journal.write(
                lambda connection: dict(self.journal.check(connection, lease))
            )
            policy = self.policy()
            policy.authorize(MaintenanceRequest.model_validate_json(row["request"]))
            if row["cancel_requested"] or policy.fingerprint != row["policy_hash"]:
                raise Denied("Maintenance authority changed before the next effect")
            try:
                result = await perform()
            except NoEffect:
                await self.journal.no_effect(lease, name)
                raise
            except Uncertain:
                await self.journal.effect_receipt(lease, name, None)
                raise
        await self.journal.effect_receipt(lease, name, result)
        return result

    async def tick(self) -> bool:
        lease = await self.journal.claim(self.worker)
        if not lease:
            return False

        async def beat() -> None:
            while True:
                await asyncio.sleep(10)
                await self.journal.heartbeat(lease)

        heartbeat = asyncio.create_task(beat())
        try:
            await self.advance(lease)
        except Uncertain as exc:
            await self.journal.transition(
                lease,
                lease.stage,
                status="uncertain",
                blocker=str(exc),
                detail={"blocker": str(exc)},
            )
        except (Denied, Conflict, OSError, TimeoutError) as exc:
            await self.journal.transition(
                lease,
                lease.stage,
                status="blocked",
                blocker=str(exc) if isinstance(exc, (Denied, Conflict)) else type(exc).__name__,
                detail={
                    "blocker": str(exc)
                    if isinstance(exc, (Denied, Conflict))
                    else type(exc).__name__
                },
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def advance(self, lease: ControllerLease) -> None:
        row = await self.journal.one("SELECT * FROM changes WHERE id=?", (lease.change_id,))
        assert row
        request = MaintenanceRequest.model_validate_json(row["request"])
        host_operation = await self.journal.receipt(lease.change_id, "activation")
        if row["cancel_requested"]:
            if host_operation:
                status = await self.host.call("status", {})
                if status.get("change_id") != lease.change_id:
                    raise Conflict("A newer activation supersedes this rollback request")
                if status["stage"] == "deployed":
                    await self.host.call("rollback", {"change_id": lease.change_id})
                elif status["stage"] not in ("rolled_back", "cancelled", "failed"):
                    await self.host.call("cancel", {"change_id": lease.change_id})
                if status["stage"] not in ("rolled_back", "cancelled", "failed"):
                    await self.journal.transition(
                        lease, lease.stage, status="waiting", detail={"recovery": status["stage"]}
                    )
                    return
                await self.journal.transition(
                    lease,
                    "rolled_back" if status["stage"] == "rolled_back" else "cancelled",
                    detail={"host": status},
                )
                return
            await self.journal.transition(lease, "cancelled")
            return
        policy = self.policy()
        # Already switched applications keep their recorded independent recovery authority.
        if not host_operation:
            policy.authorize(request)
            if policy.fingerprint != row["policy_hash"]:
                raise Denied("Standing policy changed; start a request under the current policy")
            if self.journal.clock() >= row["deadline"]:
                await self.journal.transition(
                    lease, "failed", detail={"reason": "operation_deadline"}
                )
                return
        self.github.policy = policy
        stage = lease.stage
        iteration = await self.journal.current_round(lease.change_id)
        revision = int(iteration["revision"])

        def effect_name(name: str) -> str:
            return round_effect(revision, name)

        if stage == "preparing":
            if not iteration["coding_job_id"]:
                await self.journal.transition(lease, stage, status="waiting")
                return
            storage = (self.config.root,) + (
                (self.config.bundle_root,) if self.config.bundle_root else ()
            )
            used = sum(
                path.stat().st_size
                for root in storage
                for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
            if used >= self.config.max_disk_bytes or any(
                shutil.disk_usage(root).free < 512 * 1024 * 1024
                for root in storage
                if root.exists()
            ):
                raise Denied("Maintenance storage budget or free-space reserve exhausted")
            await self.github.identity()
            fetched = await self.source.fetch(
                "https://github.com/" + policy.repository + ".git", policy.base_branch
            )
            base = await self.journal.bind_base(lease, revision, fetched)
            await git(
                self.source.cache, "merge-base", "--is-ancestor", self.config.installed_source, base
            )

            async def prepare() -> Json:
                destination = self.config.workspaces / iteration["coding_job_id"]
                if destination.exists():
                    # Editing is not admitted until this durable preparation completes.
                    shutil.rmtree(destination)
                prepared: Json = {
                    "base_commit": base,
                    "workspace_revision": revision,
                    "repair_reason": iteration["reason"],
                }
                if revision == 1:
                    await self.source.checkout(base, destination)
                else:
                    previous = await self.journal.one(
                        "SELECT identity FROM candidates WHERE change_id=? AND revision=?",
                        (lease.change_id, revision - 1),
                    )
                    assert previous
                    prepared.update(
                        await self.source.prepare_revision(
                            CandidateIdentity.model_validate_json(previous["identity"]),
                            base,
                            destination,
                        )
                    )
                if self.config.package_checks:
                    await stage_dependencies(destination / "uv.lock", self.config.dependency_wheels)
                    python = await self.verifier.prepare_environment(destination)
                    prepared["test_python"] = str(python)
                    prepared["test_environment"] = "offline, matching the selected source lock"
                await asyncio.to_thread(handoff, destination, self.config.core_gid, writable=True)
                return prepared

            prepared = await self.effect(lease, effect_name("prepare"), {"base": base}, prepare)
            await self.journal.transition(
                lease,
                "editing",
                status="waiting",
                detail={**prepared, "expected_revision": revision},
            )
            return
        if stage == "editing":
            submission = await self.journal.get_signal(lease.change_id, effect_name("submission"))
            if not submission:
                await self.journal.transition(lease, stage, status="waiting")
                return
            if submission["revision"] != revision:
                raise Conflict("Submission does not bind the issued workspace revision")
            prepared = await self.journal.receipt(lease.change_id, effect_name("prepare"))
            assert prepared
            candidate = await self.source.accept(
                lease.change_id,
                submission["revision"],
                iteration["coding_job_id"],
                prepared["base_commit"],
                submission["snapshot_sha256"],
            )
            if (
                request.origin == "autonomous"
                and candidate.revision > policy.max_candidate_revisions
            ):
                raise Denied("Candidate revision limit reached")
            await self.journal.record_candidate(lease, candidate)
            await self.journal.transition(
                lease, "verifying", detail={"candidate": candidate.model_dump(mode="json")}
            )
            return
        candidate_row = await self.journal.one(
            "SELECT identity FROM candidates WHERE change_id=? ORDER BY revision DESC LIMIT 1",
            (lease.change_id,),
        )
        assert candidate_row
        candidate = CandidateIdentity.model_validate_json(candidate_row["identity"])
        if candidate.revision != revision:
            raise Conflict("Pipeline stage does not bind the current candidate revision")
        source = self.config.root / "candidates" / lease.change_id / str(candidate.revision)
        branch = "theo/change-" + lease.change_id
        if stage == "verifying":
            await self.source.evidence(candidate)
            changed = (
                await git(source, "diff", "--name-only", candidate.base_commit, candidate.commit)
            ).splitlines()
            if request.target == "deploy" and any(
                name.startswith(("src/theo/maintenance/", ".github/workflows/"))
                or name == "src/theo/supervisor.py"
                for name in changed
            ):
                raise Denied(
                    "Control-layer or verification-policy changes require a separately staged controller handover"
                )
            await self.effect(
                lease,
                effect_name("candidate_dependencies"),
                {"lock": candidate.lock_sha256},
                lambda: stage_dependencies(source / "uv.lock", self.config.dependency_wheels),
            )
            try:
                checks = await self.effect(
                    lease,
                    effect_name("checks"),
                    candidate.model_dump(mode="json"),
                    lambda: self.verifier.check(
                        candidate, source, suffix="verify-" + str(lease.generation)
                    ),
                )
            except VerificationFailed as exc:
                await self.journal.revise(
                    lease,
                    candidate,
                    candidate.base_commit,
                    "Local verification failed: " + encode(exc.receipt),
                    policy,
                )
                return
            review = await self.journal.get_signal(lease.change_id, effect_name("review"))
            if not review:
                if not iteration["review_job_id"]:
                    raise Denied("Independent review job is missing")
                destination = self.config.workspaces / iteration["review_job_id"]

                async def prepare_review() -> Json:
                    if destination.exists():
                        await asyncio.to_thread(shutil.rmtree, destination)
                    await asyncio.to_thread(write_source, destination, read_source(source))
                    (destination / "MAINTENANCE_DIFF.txt").write_text(
                        await git(source, "diff", candidate.base_commit, candidate.commit)
                    )
                    await asyncio.to_thread(
                        handoff, destination, self.config.core_gid, writable=False
                    )
                    return {"workspace": str(destination), "candidate": candidate.commit}

                await self.effect(
                    lease,
                    effect_name("review_workspace"),
                    candidate.model_dump(mode="json"),
                    prepare_review,
                )
                await self.journal.transition(
                    lease,
                    stage,
                    status="waiting",
                    detail={
                        "candidate": candidate.model_dump(mode="json"),
                        "review_ready": True,
                        "checks": checks["checks"],
                    },
                )
                return
            if (
                review.get("candidate") != candidate.model_dump(mode="json")
                or review.get("job_id") != iteration["review_job_id"]
            ):
                raise Denied("Independent review does not bind the exact candidate and review job")
            if review.get("approved") is not True:
                await self.journal.revise(
                    lease,
                    candidate,
                    candidate.base_commit,
                    "Independent review requested repair: " + str(review.get("findings", "")),
                    policy,
                )
                return
            await self.journal.transition(
                lease, "publishing", detail={"review": review["findings"]}
            )
            return
        if stage == "publishing":

            async def push() -> Json:
                prior = await self.journal.one(
                    "SELECT receipt FROM effects WHERE change_id=? AND (name='push' OR name LIKE 'round-%:push') AND status='succeeded' ORDER BY updated_at DESC LIMIT 1",
                    (lease.change_id,),
                )
                if prior:
                    return await self.github.push(
                        source, candidate, branch, previous=json.loads(prior["receipt"])["sha"]
                    )
                return await self.github.push(source, candidate, branch)

            async def reconcile_push() -> Json | None:
                current = await self.github.ref(branch)
                return {"branch": branch, "sha": current} if current == candidate.commit else None

            await self.effect(
                lease,
                effect_name("push"),
                {"branch": branch, "sha": candidate.commit},
                push,
                reconcile_push,
            )

            async def create_pr() -> Json:
                result = await self.github.pull_request(branch, candidate, create=True)
                assert result
                return result

            pr = await self.effect(
                lease,
                effect_name("pull_request"),
                {"branch": branch, "sha": candidate.commit},
                create_pr,
                lambda: self.github.pull_request(branch, candidate, create=False),
            )
            await self.journal.transition(
                lease,
                "published" if request.target == "publish" else "awaiting_ci",
                detail={"publication": pr},
            )
            return
        pr = await self.journal.receipt(lease.change_id, effect_name("pull_request"))
        assert pr
        if stage == "awaiting_ci":
            try:
                checks = await self.github.checks(candidate.commit)
            except CheckFailed as exc:
                await self.journal.revise(lease, candidate, candidate.base_commit, str(exc), policy)
                return
            if not checks:
                await self.journal.transition(lease, stage, status="waiting")
                return
            await self.journal.transition(lease, "merging", detail=checks)
            return
        if stage == "merging":

            async def merge() -> Json:
                result = await self.github.merge(pr, candidate, send=True)
                assert result
                return result

            try:
                merged = await self.effect(
                    lease,
                    effect_name("merge"),
                    {"sha": candidate.commit, "number": pr["number"]},
                    merge,
                    lambda: self.github.merge(pr, candidate, send=False),
                )
            except BaseAdvanced:
                base = await self.github.ref(policy.base_branch)
                if not base:
                    raise Conflict("Base branch is unavailable") from None
                await self.journal.revise(lease, candidate, base, "Base branch advanced", policy)
                return
            await self.journal.transition(lease, "packaging", detail={"merged": merged})
            return
        if stage == "packaging":
            merged = await self.journal.receipt(lease.change_id, effect_name("merge"))
            assert merged
            checks = await self.github.checks(merged["sha"])
            if not checks:
                await self.journal.transition(lease, stage, status="waiting")
                return
            await self.source.fetch(
                "https://github.com/" + policy.repository + ".git", policy.base_branch
            )
            if (
                await git(self.source.cache, "rev-parse", merged["sha"] + "^{tree}")
                != candidate.tree
            ):
                raise Conflict("Merged tree differs from independently reviewed source")
            merged_candidate = candidate.model_copy(update={"commit": merged["sha"]})
            await self.effect(
                lease,
                effect_name("dependencies"),
                {"lock": candidate.lock_sha256},
                lambda: stage_dependencies(source / "uv.lock", self.config.dependency_wheels),
            )
            verification = await self.effect(
                lease,
                effect_name("merged_checks"),
                merged_candidate.model_dump(mode="json"),
                lambda: self.verifier.check(
                    merged_candidate, source, suffix="merged-" + str(lease.generation)
                ),
            )
            bundle = await self.effect(
                lease,
                effect_name("bundle"),
                {"sha": merged["sha"], "verification": digest(verification)},
                lambda: self.verifier.package(merged_candidate, source, verification),
            )
            await self.journal.transition(lease, "staged", detail={"bundle": bundle})
            return
        if stage == "staged":
            bundle = await self.journal.receipt(lease.change_id, effect_name("bundle"))
            assert bundle
            activation = await self.effect(
                lease,
                "activation",
                bundle,
                lambda: self.host.call("activate", {"change_id": lease.change_id, **bundle}),
            )
            await self.journal.transition(lease, "draining", detail={"host": activation})
            return
        status = await self.host.call("status", {})
        if status.get("verification_blocker"):
            if await self.journal.get_signal(lease.change_id, "retry"):
                await self.host.call("retry_verification", {"change_id": lease.change_id})
                await self.journal.execute(
                    "DELETE FROM signals WHERE change_id=? AND name='retry'", (lease.change_id,)
                )
                await self.journal.transition(lease, stage, status="waiting")
            else:
                await self.journal.transition(
                    lease,
                    stage,
                    status="blocked",
                    detail={"host": status, "blocker": status["verification_blocker"]},
                )
            return
        if status.get("change_id") != lease.change_id:
            raise Conflict("Host activation receipt belongs to a different change")
        if status["stage"] in ("rolled_back", "failed", "cancelled"):
            result_stage = status["stage"]
            if result_stage == "rolled_back" and stage == "draining":
                await self.journal.transition(lease, "activating", detail={"host": status})
            else:
                await self.journal.transition(
                    lease, result_stage, detail={"host": status, "publication": pr}
                )
        elif stage == "draining" and status["stage"] != "draining":
            await self.journal.transition(lease, "activating", detail={"host": status})
        elif stage == "activating" and status["stage"] in ("checking", "observing", "deployed"):
            await self.journal.transition(lease, "checking", detail={"host": status})
        elif stage == "checking" and status["stage"] in ("observing", "deployed"):
            await self.journal.transition(lease, "observing", detail={"host": status})
        elif stage == "observing" and status["stage"] == "deployed":
            await self.journal.transition(
                lease, "deployed", detail={"host": status, "publication": pr}
            )
        else:
            await self.journal.transition(lease, stage, status="waiting", detail={"host": status})


async def run(config: ControllerConfig) -> None:
    if os.geteuid() in (0, config.core_uid):
        raise Denied("Install the controller under a separate non-root service identity")
    if config.policy_uid != 0:
        raise Denied("Controller policy must be pinned to the root installation owner")
    read_operator_file(config.policy)
    # Prepared coding files must be editable by the core's shared workspace
    # group. Private controller directories themselves remain mode 0700.
    os.umask(0o007)
    config.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (config.root / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if config.vm:
            from theo.maintenance.vm_driver import reconcile

            await reconcile(config.vm)
        registry = config.root / "builder-process.json"
        if registry.exists():
            process = json.loads(registry.read_text())
            for pid, birth in (
                (process.get("child_pid"), process.get("child_birth")),
                (process.get("pid"), process.get("birth")),
            ):
                if pid and birth:
                    await asyncio.to_thread(terminate_tree, pid, created_at=birth)
        journal = Journal(config.root)
        await journal.initialize()
        controller = Controller(config, journal)
        stop = asyncio.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(signum, stop.set)
        server = await serve(
            config.socket, config.token_file, controller.rpc, expected_uid=config.core_uid
        )
        os.chown(config.socket, -1, config.core_gid)
        try:
            async with server:
                while not stop.is_set():
                    await journal.wake_waiting()
                    while await controller.tick():
                        if stop.is_set():
                            break
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), 10)
        finally:
            await controller.github.close()
            await journal.close()
            config.socket.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(ControllerConfig.model_validate_json(read_operator_file(args.config))))


if __name__ == "__main__":
    main()
