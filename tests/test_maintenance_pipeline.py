"""Real SQLite, Git, broker and Unix-socket maintenance boundary regressions."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from theo.backends.process import stop_process
from theo.config import Settings
from theo.domain import Conflict, Denied, Outcome, ToolContext, uid
from theo.maintenance.bundles import Bundle, atomic_select, inventory, verify
from theo.maintenance.configuration import CheckRecipe, ControllerConfig
from theo.maintenance.contracts import MaintenanceRequest
from theo.maintenance.controller import Controller
from theo.maintenance.github import GitHub, Uncertain
from theo.maintenance.host import Host, HostConfig
from theo.maintenance.journal import Journal
from theo.maintenance.policy import MaintenancePolicy
from theo.maintenance.rpc import Client, serve
from theo.maintenance.source import Source, git, read_source, source_digest
from theo.maintenance.verification import VerificationFailed, Verifier
from theo.tools.broker import ToolBroker
from theo.tools.registry import REGISTRY
from theo.work.jobs import Jobs
from theo.work.maintenance import Maintenance


@pytest.fixture
async def repository(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    (source / "uv.lock").write_text("version = 1\n")
    (source / "app.py").write_text("answer = 1\n")
    await git(source, "init", "-b", "main")
    await git(source, "add", "--all")
    await git(source, "commit", "-m", "Baseline")
    return source


@pytest.fixture
async def configured(tmp_path, repository):
    root = tmp_path / "controller"
    root.mkdir()
    policy = MaintenancePolicy(
        revision=1,
        owner_id="owner",
        installation_id="fixture",
        repository_id=123,
        repository="fixture/theo",
        enabled=True,
    )
    path = root / "policy.json"
    path.write_text(policy.model_dump_json())
    path.chmod(0o600)
    sockets = Path(
        tempfile.mkdtemp(
            prefix="theo-maint-test-", dir=os.environ.get("THEO_TEST_SOCKET_ROOT", "/tmp")
        )
    )
    config = ControllerConfig(
        root=root,
        policy=path,
        socket=sockets / "c.sock",
        token_file=root / "token",
        host_socket=sockets / "h.sock",
        host_token_file=root / "host-token",
        workspaces=tmp_path / "workspaces",
        installed_source=await git(repository, "rev-parse", "HEAD"),
        builder_python=Path(sys.executable),
        uv=Path("/usr/bin/true"),
        runtime_reads=(Path(sys.prefix), Path(sys.base_prefix)),
        checks=(
            CheckRecipe(
                name="behavior",
                argv=("{python}", "-c", "from app import answer; assert answer == 2"),
            ),
        ),
        required_checks={"check": 1},
        package_checks=False,  # This repository fixture is a tiny behavior test, not a Theo wheel.
        github_app_id=1,
        github_installation_id=1,
        github_key_file=root / "key",
        dependency_wheels=tmp_path / "wheels",
    )
    config.token_file.write_text("t" * 64)
    config.token_file.chmod(0o600)
    yield config, policy
    shutil.rmtree(sockets)


async def test_shared_bundle_storage_is_separate_from_controller_secrets_and_jobs(configured):
    config, _ = configured
    shared = config.root.parent / "accepted-bundles"
    selected = ControllerConfig.model_validate({**config.model_dump(), "bundle_root": shared})
    assert selected.bundles == shared
    for unsafe in (config.root / "bundles", config.workspaces / "bundles", config.root.parent):
        with pytest.raises(ValidationError, match="Shared bundles"):
            ControllerConfig.model_validate({**config.model_dump(), "bundle_root": unsafe})


async def broker_context(db, settings, conversation, workspace, kind="conversation", job_id=None):
    jobs = Jobs(db, settings.owner_id)
    job_id = job_id or await jobs.enqueue(
        conversation, kind, {"text": "fix"}, uid(), lane="interactive"
    )
    job = await jobs.claim("interactive" if kind == "conversation" else "background", "fixture")
    assert job and job["id"] == job_id
    run_id = uid()
    await db.execute(
        "INSERT INTO runs(id,owner_id,job_id,generation,backend,model,status,started_at) VALUES(?,?,?,?,?,?,?,?)",
        (run_id, "owner", job_id, job["generation"], "codex", "fixture", "running", db.clock()),
    )
    context = ToolContext(
        owner_id="owner",
        conversation_id=conversation,
        job_id=job_id,
        run_id=run_id,
        generation=job["generation"],
        workspace=workspace,
        tools=frozenset(REGISTRY),
    )
    broker = ToolBroker(db, settings)
    return broker, broker.grant(context), context


async def test_broker_file_writes_preserve_shared_access_and_durable_receipts(
    db, conversation, tmp_path
):
    workspace = tmp_path / "shared-job"
    workspace.mkdir()
    workspace.chmod(0o770)
    broker, token, context = await broker_context(db, Settings(), conversation, workspace)
    try:
        arguments = {"path": "new/package.py", "content": "answer = 7\n"}
        previous = os.umask(0o077)
        try:
            result = await broker.call(token, "file_write", arguments)
            assert result.status == "committed"
            assert await broker.call(token, "file_write", arguments) == result
        finally:
            os.umask(previous)
        assert (workspace / "new").stat().st_mode & 0o777 == 0o770
        assert (workspace / "new/package.py").stat().st_mode & 0o777 == 0o660
        receipt = await db.one(
            "SELECT count(*) n FROM tool_receipts WHERE job_id=?", (context.job_id,)
        )
        assert receipt["n"] == 1
        read = await broker.call(token, "file_read", {"path": arguments["path"]})
        assert read.status == "ok" and read.data["content"] == arguments["content"]
        private = db.root / "private-fixture"
        private.write_text("synthetic private data")
        (workspace / "redirect").symlink_to(private)
        assert (await broker.call(token, "file_read", {"path": "redirect"})).status == "denied"
    finally:
        await broker.close()


async def test_broker_to_real_source_submit_and_durable_projection(
    db, conversation, configured, repository, monkeypatch
):
    config, policy = configured
    journal = Journal(config.root)
    await journal.initialize()
    controller = Controller(config, journal)
    original_fetch = controller.source.fetch

    async def fetch(_url, branch):
        return await original_fetch(str(repository), branch)

    async def identity():
        return None

    monkeypatch.setattr(controller.source, "fetch", fetch)
    monkeypatch.setattr(controller.github, "identity", identity)
    server = await serve(config.socket, config.token_file, controller.rpc)
    settings = Settings(
        worker_home=config.workspaces.parent,
        maintenance_socket=config.socket,
        maintenance_token_file=config.token_file,
        maintenance_installation_id="fixture",
    )
    # Explicitly use the same host-issued workspace directory as the controller.
    settings = settings.model_copy(update={"worker_home": config.workspaces.parent})
    broker, token, context = await broker_context(
        db, settings, conversation, config.workspaces / "owner"
    )
    try:
        args = {"objective": "Change answer to 2", "target": "publish"}
        started = await broker.call(token, "maintenance_begin", args)
        assert started.status == "committed", started
        assert await broker.call(token, "maintenance_begin", args) == started
        assert len(await db.read("SELECT * FROM maintenance_intents")) == 1
        intent = await db.one("SELECT * FROM maintenance_intents")
        assert (await db.one("SELECT status FROM jobs WHERE id=?", (intent["coding_job_id"],)))[
            "status"
        ] == "waiting_for_dependency"
        await Jobs(db, "owner").finish(context.job_id, context.generation, Outcome.COMPLETED, {})
        maintenance = Maintenance(db, settings)
        await maintenance.bridge()
        assert await controller.tick()
        await maintenance.bridge()
        intent = await db.one("SELECT * FROM maintenance_intents")
        workspace = config.workspaces / intent["coding_job_id"]
        assert (workspace / "app.py").read_text() == "answer = 1\n"
        coding_broker, coding_token, coding = await broker_context(
            db, settings, conversation, workspace, "maintenance_code", intent["coding_job_id"]
        )
        try:
            assert (
                await coding_broker.call(
                    coding_token, "file_write", {"path": "app.py", "content": "answer = 2\n"}
                )
            ).status == "committed"
            submitted = await coding_broker.call(
                coding_token,
                "maintenance_submit",
                {"change_id": intent["change_id"], "expected_revision": 1, "summary": "Fix answer"},
            )
            assert submitted.status == "committed", submitted
            assert (
                await coding_broker.call(
                    coding_token, "file_write", {"path": "app.py", "content": "answer = 3\n"}
                )
            ).status == "denied"
            await Jobs(db, "owner").finish(coding.job_id, coding.generation, Outcome.COMPLETED, {})
            await maintenance.bridge()
            assert await controller.tick()
            candidate = await journal.one("SELECT identity FROM candidates")
            identity = json.loads(candidate["identity"])
            accepted = config.root / "candidates" / intent["change_id"] / "1"
            assert (accepted / "app.py").read_text() == "answer = 2\n"
            assert await git(accepted, "rev-parse", "HEAD") == identity["commit"]
            # Lost bridge acknowledgement replays no new controller change or candidate.
            await maintenance.bridge()
            assert len(await journal.read("SELECT * FROM changes")) == 1
            assert len(await journal.read("SELECT * FROM candidates")) == 1
        finally:
            await coding_broker.close()
    finally:
        server.close()
        await server.wait_closed()
        await broker.close()
        await controller.github.close()
        await journal.close()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "credential", "private"])
def test_source_rejects_unsafe_publication(tmp_path, kind):
    workspace = tmp_path / "work"
    workspace.mkdir()
    target = workspace / "source.py"
    if kind == "symlink":
        target.symlink_to("/etc/passwd")
    elif kind == "hardlink":
        outside = tmp_path / "outside"
        outside.write_text("private")
        os.link(outside, target)
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "credential":
        target.write_text("-----BEGIN PRIVATE KEY-----\ncredential\n")
    else:
        (workspace / ".env").write_text("private")
    with pytest.raises((Denied, OSError)):
        read_source(workspace)


async def test_submission_detects_changed_bytes(configured, repository):
    config, _ = configured
    source = Source(config.root, config.workspaces)
    base = await source.fetch(str(repository), "main")
    workspace = config.workspaces / "coding"
    await source.checkout(base, workspace)
    checksum = source_digest(workspace)
    (workspace / "app.py").write_text("answer = 99\n")
    with pytest.raises(Conflict, match="changed"):
        await source.accept("change", 1, "coding", base, checksum)


@pytest.mark.skipif(sys.platform != "darwin", reason="Real generated-code sandbox is macOS only")
async def test_real_verifier_denies_private_reads_and_network(configured, tmp_path):
    config, _ = configured
    workspace = config.workspaces / "sandbox"
    workspace.mkdir(parents=True)
    secret = config.root / "private-secret"
    secret.write_text("secret")
    probe = (
        "import pathlib,socket; denied=0\ntry: pathlib.Path("
        + repr(str(secret))
        + ").read_text()\nexcept PermissionError: denied+=1\ntry: socket.socket().connect(('127.0.0.1',9))\nexcept PermissionError: denied+=1\nassert denied==2"
    )
    result = await Verifier(config).command(
        workspace, CheckRecipe(name="isolation", argv=("{python}", "-c", probe))
    )
    assert result["exit_code"] == 0
    assert secret.read_text() == "secret"


def create_bundle(root, name, source="a" * 40, schema=9):
    target = root / name
    for part in ("core", "worker"):
        (target / part / "bin").mkdir(parents=True)
        (target / part / "bin/python").write_text("fixture interpreter")
    bundle = Bundle(
        bundle_id=name,
        source_sha=source,
        tree="b" * 40,
        schema_min=schema,
        schema_max=schema,
        files=inventory(target),
        verification_hash="c" * 64,
    )
    (target / "bundle.json").write_text(bundle.model_dump_json())
    return target, bundle


def test_bundle_manifest_rejects_unlisted_or_changed_content(tmp_path):
    path, bundle = create_bundle(tmp_path, "new")
    assert verify(path, bundle.fingerprint) == bundle
    (path / "unlisted").write_text("extra code")
    with pytest.raises(Denied, match="manifest"):
        verify(path)


def test_atomic_bundle_selection_requires_expected_previous(tmp_path):
    old, _ = create_bundle(tmp_path, "old")
    new, _ = create_bundle(tmp_path, "new")
    pointer = tmp_path / "current"
    atomic_select(pointer, old, None)
    with pytest.raises(Conflict):
        atomic_select(pointer, new, None)
    assert pointer.resolve() == old
    atomic_select(pointer, new, old)
    assert pointer.resolve() == new


async def test_supervisor_selection_is_independent_of_the_core_release_projection(db, configured):
    from theo.maintenance.bundles import selected_configuration

    config, _ = configured
    old, _ = create_bundle(config.root / "bundles", "old")
    new, _ = create_bundle(config.root / "bundles", "new")
    selection = db.root.parent / "protected-selection/current"
    atomic_select(selection, old, None)
    atomic_select(db.root / "releases/current", new, None)
    host = Host(
        HostConfig(
            root=db.root,
            state_root=db.root.parent / "host",
            selection=selection,
            bundles=config.root / "bundles",
            socket=config.host_socket,
            token_file=config.host_token_file,
        )
    )
    try:
        assert host.active() == old
        settings = selected_configuration(db.root, selected=host.active())
        assert settings["worker_python"] == old / "worker/bin/python"
        selection.unlink()
        selection.symlink_to(db.root)
        with pytest.raises(Denied, match="accepted installation"):
            host.active()
        with pytest.raises(Denied, match="unavailable"):
            selected_configuration(db.root, selected=db.root / "missing")
    finally:
        await host.db.close()


@pytest.mark.skipif(
    os.geteuid() == 0, reason="Root-to-service-UID launch needs a provisioned account"
)
async def test_supervisor_launches_selected_core_without_inheriting_control_credentials(
    db, configured, monkeypatch
):
    config, _ = configured
    target, descriptor = create_bundle(config.root / "bundles", "selected")
    result = db.root / "launch-proof.json"
    executable = target / "core/bin/python"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,os\n"
        "from pathlib import Path\n"
        f"Path({str(result)!r}).write_text(json.dumps(dict(os.environ),sort_keys=True))\n"
    )
    executable.chmod(0o755)
    descriptor = descriptor.model_copy(update={"files": inventory(target)})
    (target / "bundle.json").write_text(descriptor.model_dump_json())
    selection = db.root.parent / "protected-selection/current"
    atomic_select(selection, target, None)
    credential = db.root.parent / "synthetic-telegram-token"
    credential.write_text("synthetic-token")
    credential.chmod(0o600)

    def read_fixture(path, *, private=False):
        assert path == credential and private
        return path.read_text()

    # Root-owned credential provisioning is tested separately; this real
    # subprocess exercises only the selected executable and environment handoff.
    monkeypatch.setattr("theo.maintenance.host.read_operator_file", read_fixture)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-controller-credential")
    monkeypatch.setenv("UNRECOGNIZED_SECRET", "synthetic-private-value")
    monkeypatch.setenv("PYTHONPATH", "/synthetic-untrusted-startup")
    state = db.root.parent / "host"
    state.mkdir()
    host = Host(
        HostConfig(
            root=db.root,
            state_root=state,
            selection=selection,
            bundles=config.root / "bundles",
            socket=config.host_socket,
            token_file=config.host_token_file,
            telegram_token_file=credential,
        )
    )
    try:
        await host.start()
        assert host.child is not None
        assert await asyncio.wait_for(host.child.wait(), 10) == 0
        environment = json.loads(result.read_text())
        assert environment["THEO_SELECTED_BUNDLE"] == str(target)
        assert environment["THEO_TELEGRAM_TOKEN"] == "synthetic-token"
        assert not {"GITHUB_TOKEN", "UNRECOGNIZED_SECRET", "PYTHONPATH"} & environment.keys()
        identity = json.loads((state / "core-process.json").read_text())
        assert identity["python"] == str(executable)
        assert identity["pid"] == host.child.pid
    finally:
        if host.child:
            await stop_process(host.child)
        await host.db.close()


async def test_host_recovers_pointer_switch_without_controller(db, configured, monkeypatch, clock):
    config, _ = configured
    old, _ = create_bundle(config.root / "bundles", "old")
    new, bundle = create_bundle(config.root / "bundles", "new")
    pointer = db.root / "releases/current"
    atomic_select(pointer, old, None)
    host_config = HostConfig(
        root=db.root,
        state_root=db.root.parent / "host",
        bundles=config.root / "bundles",
        socket=db.root / "h.sock",
        token_file=config.host_token_file,
    )
    host = Host(host_config)
    await host.db.close()
    host.db = db
    host.save(
        stage="activating",
        change_id="change",
        bundle_id="new",
        fingerprint=bundle.fingerprint,
        previous=str(old),
        previous_fingerprint=verify(old).fingerprint,
        cancellation=False,
    )
    # Simulate process death in the gap after replace and before host receipt.
    atomic_select(pointer, new, old)
    recovered = Host(host_config)
    await recovered.db.close()
    recovered.db = db
    started = []

    async def start():
        started.append(recovered.active().name)

    async def stop():
        return None

    async def canary():
        return True

    monkeypatch.setattr(recovered, "start", start)
    monkeypatch.setattr(recovered, "stop", stop)
    monkeypatch.setattr(recovered, "canary", canary)
    monkeypatch.setattr(recovered, "deterministic_health", canary)
    await recovered.tick()
    assert recovered.state["stage"] == "checking" and started == ["new"]
    recovered.save(stage="recovering")
    await db.set_control("owner", "models_paused", "true")
    await recovered.tick()
    assert pointer.resolve() == old and recovered.state["stage"] == "recovery_check"
    await recovered.tick()
    assert recovered.state["stage"] == "rolled_back"
    assert await db.control("owner", "models_paused") == "true"
    assert (await db.one("SELECT max(version) n FROM schema_migrations"))["n"] == 9


async def test_ambiguous_effect_is_reconciled_without_repeating_mutation(configured):
    config, policy = configured
    journal = Journal(config.root)
    await journal.initialize()
    controller = Controller(config, journal)
    request = MaintenanceRequest(
        request_id="req",
        owner_id="owner",
        installation_id="fixture",
        job_id="parent",
        coding_job_id="code",
        conversation_id="conv",
        origin="requested",
        objective="fix",
        target="publish",
    )
    await journal.accept(policy, request)
    lease = await journal.claim("publisher")
    calls = []

    async def mutation():
        calls.append("effect")
        raise Uncertain("lost response")

    async def observation():
        return {"sha": "a" * 40}

    try:
        with pytest.raises(Uncertain):
            await controller.effect(lease, "push", {"sha": "a" * 40}, mutation, observation)
        assert await controller.effect(lease, "push", {"sha": "a" * 40}, mutation, observation) == {
            "sha": "a" * 40
        }
        assert calls == ["effect"]
        assert (await journal.receipt(lease.change_id, "push"))["sha"] == "a" * 40
    finally:
        await controller.github.close()
        await journal.close()


@pytest.mark.parametrize("conclusion", ["skipped", "neutral", "failure", "cancelled"])
async def test_github_checks_reject_non_success_conclusions(configured, conclusion):
    config, policy = configured
    github = GitHub(config, policy)

    async def api(_method, path, _body=None):
        if "check-runs" in path:
            return {
                "total_count": 1,
                "check_runs": [
                    {
                        "id": 1,
                        "name": "check",
                        "app": {"id": 1},
                        "status": "completed",
                        "head_sha": "a" * 40,
                        "conclusion": conclusion,
                        "check_suite": {"id": 42},
                    }
                ],
            }
        return {
            "workflow_runs": [
                {
                    "check_suite_id": 42,
                    "head_sha": "a" * 40,
                    "path": config.required_workflow,
                    "conclusion": "success",
                    "event": "push",
                }
            ]
        }

    github.api = api
    try:
        with pytest.raises(Denied, match="check"):
            await github.checks("a" * 40)
    finally:
        await github.close()


async def test_unix_rpc_rejects_bad_credential_and_duplicate_listener(configured):
    config, _ = configured

    async def echo(operation, body):
        return {"operation": operation, **body}

    server = await serve(config.socket, config.token_file, echo)
    try:
        assert (await Client(config.socket, config.token_file).call("probe", {"n": 1}))["n"] == 1
        bad = config.root / "bad-token"
        bad.write_text("wrong")
        with pytest.raises(Denied, match="authentication"):
            await Client(config.socket, bad).call("probe", {})
        with pytest.raises(Conflict, match="already"):
            await serve(config.socket, config.token_file, echo)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Publication fixture runs real Mac verification"
)
async def test_complete_publish_with_real_git_and_independent_review(
    configured, repository, monkeypatch
):
    config, policy = configured
    journal = Journal(config.root)
    await journal.initialize()
    controller = Controller(config, journal)
    original_fetch = controller.source.fetch

    async def fetch(_url, branch):
        return await original_fetch(str(repository), branch)

    async def identity():
        return None

    async def dependencies(_lock, _destination):
        return {"fixture": "no dependencies"}

    async def environment(_workspace):
        return config.builder_python

    async def push(path, candidate, branch):
        await git(path, "push", str(repository), candidate.commit + ":refs/heads/" + branch)
        return {"branch": branch, "sha": await git(repository, "rev-parse", "refs/heads/" + branch)}

    published = []

    async def pr(branch, candidate, *, create):
        if create:
            published.append(candidate.commit)
        return {
            "number": 1,
            "url": "https://github.com/fixture/theo/pull/1",
            "sha": candidate.commit,
            "branch": branch,
        }

    monkeypatch.setattr(controller.source, "fetch", fetch)
    monkeypatch.setattr(controller.github, "identity", identity)
    monkeypatch.setattr(controller.github, "push", push)
    monkeypatch.setattr(controller.github, "pull_request", pr)
    monkeypatch.setattr(controller.verifier, "prepare_environment", environment)
    monkeypatch.setattr("theo.maintenance.controller.stage_dependencies", dependencies)
    request = MaintenanceRequest(
        request_id="publish-fixture",
        owner_id="owner",
        installation_id="fixture",
        job_id="owner-job",
        coding_job_id="coding",
        review_job_id="review",
        conversation_id="private",
        origin="requested",
        objective="Change answer to 2",
        target="publish",
    )
    try:
        change = await journal.accept(policy, request)
        assert await controller.tick()
        workspace = config.workspaces / "coding"
        (workspace / "app.py").write_text("answer = 2\n")
        await journal.signal(
            change["id"],
            "submission",
            {"revision": 1, "snapshot_sha256": source_digest(workspace), "summary": "Fix answer"},
        )
        while await controller.tick():
            pass
        state = await journal.one("SELECT * FROM changes")
        assert state["stage"] == "verifying" and state["status"] == "waiting", state
        candidate = json.loads((await journal.one("SELECT identity FROM candidates"))["identity"])
        assert await journal.receipt(change["id"], "checks")
        assert not published
        assert (config.workspaces / "review/MAINTENANCE_DIFF.txt").is_file()
        await journal.signal(
            change["id"],
            "review",
            {
                "candidate": candidate,
                "approved": True,
                "job_id": "review",
                "run_id": "review-attempt",
                "findings": "Inspected the exact diff; fixture behavior is correct.",
            },
        )
        while await controller.tick():
            pass
        state = await journal.one("SELECT * FROM changes")
        assert state["stage"] == "published" and state["status"] == "terminal", state
        assert (
            await git(repository, "show", "theo/change-" + change["id"] + ":app.py") == "answer = 2"
        )
        assert await git(repository, "show", "main:app.py") == "answer = 1"
        assert published == [candidate["commit"]]
        assert not await journal.receipt(change["id"], "activation")
    finally:
        await controller.github.close()
        await journal.close()


async def test_private_coding_job_cannot_approve_its_own_candidate(db, configured, conversation):
    config, _ = configured
    settings = Settings(
        worker_home=config.workspaces.parent,
        maintenance_socket=config.socket,
        maintenance_token_file=config.token_file,
        maintenance_installation_id="fixture",
    )
    broker, token, context = await broker_context(
        db, settings, conversation, config.workspaces / "owner"
    )
    try:
        started = await broker.call(
            token, "maintenance_begin", {"objective": "fix", "target": "publish"}
        )
        assert started.status == "committed"
        await db.execute("UPDATE maintenance_intents SET change_id='change'")
        result = await broker.call(
            token,
            "maintenance_review",
            {
                "change_id": "change",
                "candidate": {
                    "change_id": "change",
                    "revision": 1,
                    "base_commit": "a" * 40,
                    "commit": "b" * 40,
                    "tree": "c" * 40,
                    "snapshot_sha256": "d" * 64,
                    "lock_sha256": "e" * 64,
                },
                "approved": True,
                "findings": "I approve my own code",
            },
        )
        assert result.status == "denied"
        assert not (await db.one("SELECT review FROM maintenance_intents"))["review"]
    finally:
        await broker.close()


async def test_quota_wait_preserves_running_release_without_false_rollback(
    db, configured, monkeypatch
):
    from theo.maintenance.host import CanaryUnavailable

    config, _ = configured
    old, _ = create_bundle(config.root / "bundles", "old")
    atomic_select(db.root / "releases/current", old, None)
    host = Host(
        HostConfig(
            root=db.root,
            state_root=db.root.parent / "host",
            bundles=config.root / "bundles",
            socket=db.root / "h.sock",
            token_file=config.host_token_file,
        )
    )
    await host.db.close()
    host.db = db
    host.save(stage="checking", deadline=db.clock() + 120)

    async def canary():
        raise CanaryUnavailable("waiting_for_quota")

    monkeypatch.setattr(host, "canary", canary)
    await host.tick()
    assert host.state["stage"] == "checking"
    assert host.state["verification_blocker"] == "waiting_for_quota"
    assert host.pointer.resolve() == old
    assert await db.control("owner", "maintenance_draining") == "false"


def test_dependency_requests_reject_arbitrary_hosts_and_unlocked_sources():
    from theo.maintenance.dependencies import wheel_requests

    with pytest.raises(Denied, match="PyPI"):
        wheel_requests(b'[[package]]\nname="bad"\nsource={git="https://example.com/x"}\n')
    with pytest.raises(Denied, match="host"):
        wheel_requests(
            b'[[package]]\nname="bad"\nsource={registry="https://pypi.org/simple"}\nwheels=[{url="https://example.com/bad-1.0-py3-none-any.whl",hash="sha256:0000",size=10}]\n'
        )


async def test_candidate_commit_recovers_after_copy_before_git_commit(configured, repository):
    config, _ = configured
    source = Source(config.root, config.workspaces)
    base = await source.fetch(str(repository), "main")
    workspace = config.workspaces / "coding"
    await source.checkout(base, workspace)
    (workspace / "app.py").write_text("answer = 2\n")
    expected = source_digest(workspace)
    candidate_path = config.root / "candidates/change/1"
    await source.checkout(base, candidate_path)
    (candidate_path / "app.py").write_text("answer = 2\n")
    # Process died after copying bytes, so the old HEAD must not become its receipt.
    assert await git(candidate_path, "rev-parse", "HEAD") == base
    candidate = await source.accept("change", 1, "coding", base, expected)
    assert candidate.commit != base
    assert await git(candidate_path, "show", candidate.commit + ":app.py") == "answer = 2"
    assert await source.accept("change", 1, "coding", base, expected) == candidate


async def test_submission_includes_files_ignored_by_candidate(configured, repository):
    config, _ = configured
    source = Source(config.root, config.workspaces)
    base = await source.fetch(str(repository), "main")
    workspace = config.workspaces / "coding"
    await source.checkout(base, workspace)
    (workspace / ".gitignore").write_text("new.py\n")
    (workspace / "new.py").write_text("answer = 2\n")
    candidate = await source.accept("change", 1, "coding", base, source_digest(workspace))
    assert (
        await git(config.root / "candidates/change/1", "show", candidate.commit + ":new.py")
        == "answer = 2"
    )


async def test_policy_revoked_between_effects_stops_next_mutation(configured):
    config, policy = configured
    journal = Journal(config.root)
    await journal.initialize()
    controller = Controller(config, journal)
    request = MaintenanceRequest(
        request_id="revocation",
        owner_id="owner",
        installation_id="fixture",
        job_id="parent",
        coding_job_id="coding",
        conversation_id="private",
        origin="requested",
        objective="fix",
        target="publish",
    )
    await journal.accept(policy, request)
    lease = await journal.claim("controller")
    calls = []

    async def effect():
        calls.append("effect")
        return {"observed": True}

    try:
        await controller.effect(lease, "first", {}, effect)
        config.policy.write_text(
            policy.model_copy(update={"enabled": False, "revision": 2}).model_dump_json()
        )
        with pytest.raises(Denied):
            await controller.effect(lease, "second", {}, effect)
        assert calls == ["effect"]
        assert await journal.receipt(lease.change_id, "first") == {"observed": True}
        assert not await journal.receipt(lease.change_id, "second")
    finally:
        await controller.github.close()
        await journal.close()


async def test_unix_rpc_rejects_wrong_os_identity_even_with_valid_secret(configured):
    config, _ = configured
    calls = []

    async def handler(operation, body):
        calls.append(operation)
        return body

    server = await serve(config.socket, config.token_file, handler, expected_uid=os.geteuid() + 1)
    try:
        with pytest.raises((Denied, ConnectionError), match="identity|reset|Broken pipe"):
            await Client(config.socket, config.token_file).call("probe", {})
        assert not calls
    finally:
        server.close()
        await server.wait_closed()


def test_controller_rejects_private_files_disguised_as_build_outputs(tmp_path):
    from theo.maintenance.verification import output_file

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "controller-private"
    secret.write_text("synthetic-private-key")
    linked = workspace / "candidate.whl"
    linked.symlink_to(secret)
    with pytest.raises(Denied, match="escapes"):
        output_file(linked, workspace)
    linked.unlink()
    os.link(secret, linked)
    with pytest.raises(Denied, match="without links"):
        output_file(linked, workspace)


@pytest.mark.skipif(sys.platform != "darwin", reason="Real Mac filesystem capability check")
async def test_candidate_cannot_choose_its_next_temporary_directory(configured):
    config, _ = configured
    workspace = config.workspaces / "temporary-forgery"
    (workspace / ".theo").mkdir(parents=True)
    (workspace / ".theo/temporary.json").write_text(json.dumps({"path": str(config.root)}))
    secret = config.root / "private"
    secret.write_text("synthetic-private-key")
    with pytest.raises(Denied, match="Verification failed"):
        await Verifier(config).command(
            workspace,
            CheckRecipe(
                name="temporary-forgery",
                argv=(
                    "{python}",
                    "-c",
                    "from pathlib import Path; Path(" + repr(str(secret)) + ").read_text()",
                ),
            ),
        )
    assert secret.read_text() == "synthetic-private-key"


@pytest.mark.parametrize("conflict", [False, True])
async def test_revision_integrates_upstream_and_preserves_prior_source(
    configured, repository, conflict
):
    config, _ = configured
    source = Source(config.root, config.workspaces)
    base = await source.fetch(str(repository), "main")
    workspace = config.workspaces / "coding"
    await source.checkout(base, workspace)
    # A trailing space in the final patch line must survive Git output handling.
    (workspace / "app.py").write_text("answer = 2  \n")
    candidate = await source.accept("change", 1, "coding", base, source_digest(workspace))
    (repository / "upstream.txt").write_text("upstream work\n")
    if conflict:
        (repository / "app.py").write_text("answer = 3\n")
    await git(repository, "add", "--all")
    await git(repository, "commit", "-m", "Upstream change")
    updated = await source.fetch(str(repository), "main")
    destination = config.workspaces / "coding-2"
    receipt = await source.prepare_revision(candidate, updated, destination)
    assert (destination / "upstream.txt").read_text() == "upstream work\n"
    assert (destination / ".theo/previous.patch").is_file()
    assert receipt["integration"] == ("needs_resolution" if conflict else "applied")
    if conflict:
        assert "<<<<<<<" in (destination / "app.py").read_text()
        (destination / "app.py").write_text("answer = 5\n")
    else:
        assert (destination / "app.py").read_text() == "answer = 2  \n"
    revised = await source.accept("change", 2, "coding-2", updated, source_digest(destination))
    assert revised.base_commit == updated and revised.commit != candidate.commit
    assert source_digest(config.root / "candidates/change/1") == candidate.snapshot_sha256
    assert await git(config.root / "candidates/change/2", "rev-parse", "HEAD^") == updated


@pytest.mark.parametrize("repair_kind", ["check", "review"])
async def test_repair_round_creates_fresh_broker_jobs_and_invalidates_old_evidence(
    db, conversation, configured, repository, monkeypatch, repair_kind
):
    config, policy = configured
    journal = Journal(config.root)
    await journal.initialize()
    controller = Controller(config, journal)
    original_fetch = controller.source.fetch

    async def fetch(_url, branch):
        return await original_fetch(str(repository), branch)

    async def identity():
        return None

    async def dependencies(_lock, _destination):
        return {"fixture": "no dependencies"}

    checked = []

    async def check(candidate, source, *, suffix):
        assert source_digest(source) == candidate.snapshot_sha256
        checked.append(candidate.commit)
        if repair_kind == "check" and candidate.revision == 1:
            raise VerificationFailed({"name": "behavior", "exit_code": 1, "log_sha256": "a" * 64})
        return {"checks": [{"name": "fixture", "sha": candidate.commit, "exit_code": 0}]}

    monkeypatch.setattr(controller.source, "fetch", fetch)
    monkeypatch.setattr(controller.github, "identity", identity)
    monkeypatch.setattr(controller.verifier, "check", check)
    monkeypatch.setattr("theo.maintenance.controller.stage_dependencies", dependencies)
    server = await serve(config.socket, config.token_file, controller.rpc)
    settings = Settings(
        worker_home=config.workspaces.parent,
        maintenance_socket=config.socket,
        maintenance_token_file=config.token_file,
        maintenance_installation_id="fixture",
    )
    owner_broker, owner_token, owner = await broker_context(
        db, settings, conversation, config.workspaces / "owner"
    )
    brokers = [owner_broker]
    try:
        started = await owner_broker.call(
            owner_token, "maintenance_begin", {"objective": "Change answer", "target": "publish"}
        )
        assert started.status == "committed"
        await Jobs(db, "owner").finish(owner.job_id, owner.generation, Outcome.COMPLETED, {})
        maintenance = Maintenance(db, settings)
        await maintenance.bridge()
        assert await controller.tick()
        await maintenance.bridge()
        first = await db.one("SELECT * FROM maintenance_intents")
        workspace = config.workspaces / first["coding_job_id"]
        broker, token, context = await broker_context(
            db, settings, conversation, workspace, "maintenance_code", first["coding_job_id"]
        )
        brokers.append(broker)
        (workspace / "app.py").write_text("answer = 2\n")
        assert (
            await broker.call(
                token,
                "maintenance_submit",
                {
                    "change_id": first["change_id"],
                    "expected_revision": 1,
                    "summary": "First attempt",
                },
            )
        ).status == "committed"
        await Jobs(db, "owner").finish(context.job_id, context.generation, Outcome.COMPLETED, {})
        await maintenance.bridge()
        assert await controller.tick()  # accept candidate
        assert await controller.tick()  # observe check result
        await maintenance.bridge()
        candidate1 = json.loads((await journal.one("SELECT identity FROM candidates"))["identity"])
        if repair_kind == "review":
            reviewer, review_token, review_context = await broker_context(
                db,
                settings,
                conversation,
                config.workspaces / first["review_job_id"],
                "maintenance_review",
                first["review_job_id"],
            )
            brokers.append(reviewer)
            assert (
                await reviewer.call(
                    review_token,
                    "maintenance_review",
                    {
                        "change_id": first["change_id"],
                        "candidate": candidate1,
                        "approved": False,
                        "findings": "Please handle the missing case.",
                    },
                )
            ).status == "committed"
            # Leave the reviewer running to prove that round replacement fences it.
            await maintenance.bridge()
            assert await controller.tick()
            await maintenance.bridge()
            assert (await reviewer.call(review_token, "maintenance_status", {})).status == "denied"
        second = await db.one("SELECT * FROM maintenance_intents")
        assert second["revision"] == 2
        assert second["coding_job_id"] != first["coding_job_id"]
        assert second["review_job_id"] != first["review_job_id"]
        assert second["submission"] is None and second["review"] is None
        assert "candidate" not in json.loads(second["projection"])
        archived = await db.one("SELECT * FROM maintenance_rounds WHERE revision=1")
        assert archived["submission"] and bool(archived["review"]) == (repair_kind == "review")
        # Replaying old events and old signals cannot allocate more jobs or restore old reviews.
        await maintenance.bridge()
        await maintenance.bridge()
        assert len(await db.read("SELECT * FROM maintenance_rounds")) == 2
        assert await controller.tick()  # prepare issued round with its new jobs
        await maintenance.bridge()
        workspace2 = config.workspaces / second["coding_job_id"]
        assert (workspace2 / "app.py").read_text() == "answer = 2\n"
        broker2, token2, context2 = await broker_context(
            db, settings, conversation, workspace2, "maintenance_code", second["coding_job_id"]
        )
        brokers.append(broker2)
        assert (
            await broker2.call(token2, "file_write", {"path": "app.py", "content": "answer = 4\n"})
        ).status == "committed"
        assert (
            await broker2.call(
                token2,
                "maintenance_submit",
                {"change_id": first["change_id"], "expected_revision": 2, "summary": "Repair"},
            )
        ).status == "committed"
        await Jobs(db, "owner").finish(context2.job_id, context2.generation, Outcome.COMPLETED, {})
        await maintenance.bridge()
        assert await controller.tick()
        assert await controller.tick()
        await maintenance.bridge()
        latest = await db.one("SELECT * FROM maintenance_intents")
        candidate2 = json.loads(latest["projection"])["candidate"]
        assert candidate2["revision"] == 2 and candidate2["commit"] != candidate1["commit"]
        assert checked == [candidate1["commit"], candidate2["commit"]]
        assert (await journal.one("SELECT stage,status FROM changes")) == {
            "stage": "verifying",
            "status": "waiting",
        }
        assert await journal.get_signal(first["change_id"], "round-2:review") is None
        # A new reviewer cannot apply the old verdict to the new identity.
        reviewer2, review_token2, _ = await broker_context(
            db,
            settings,
            conversation,
            config.workspaces / second["review_job_id"],
            "maintenance_review",
            second["review_job_id"],
        )
        brokers.append(reviewer2)
        assert (
            await reviewer2.call(
                review_token2,
                "maintenance_review",
                {
                    "change_id": first["change_id"],
                    "candidate": candidate1,
                    "approved": True,
                    "findings": "Stale verdict",
                },
            )
        ).status == "revision_conflict"
        assert not (await db.one("SELECT review FROM maintenance_intents"))["review"]
        assert await journal.receipt(first["change_id"], "round-2:checks")
        assert (
            await reviewer2.call(
                review_token2,
                "maintenance_review",
                {
                    "change_id": first["change_id"],
                    "candidate": candidate2,
                    "approved": True,
                    "findings": "Reviewed the repaired candidate.",
                },
            )
        ).status == "committed"
        await maintenance.bridge()
        assert await controller.tick()
        assert (await journal.one("SELECT stage FROM changes"))["stage"] == "publishing"
    finally:
        server.close()
        await server.wait_closed()
        for broker in brokers:
            await broker.close()
        await controller.github.close()
        await journal.close()


@pytest.mark.parametrize("merge_state", ["base_advanced", "unknown", "already_merged"])
async def test_controller_integrates_changed_base_only_after_resolving_merge_outcome(
    configured, repository, monkeypatch, merge_state
):
    config, policy = configured
    journal = Journal(config.root)
    await journal.initialize()
    controller = Controller(config, journal)
    request = MaintenanceRequest(
        request_id="merge-fixture",
        owner_id="owner",
        installation_id="fixture",
        job_id="owner",
        coding_job_id="coding",
        review_job_id="review",
        conversation_id="private",
        origin="requested",
        objective="Repair",
        target="deploy",
    )
    accepted = await journal.accept(policy, request)
    base = await controller.source.fetch(str(repository), "main")
    preparing = await journal.claim("fixture")
    await journal.bind_base(preparing, 1, base)
    await journal.transition(preparing, "editing")
    workspace = config.workspaces / "coding"
    await controller.source.checkout(base, workspace)
    (workspace / "app.py").write_text("answer = 2\n")
    candidate = await controller.source.accept(
        accepted["id"], 1, "coding", base, source_digest(workspace)
    )
    editing = await journal.claim("fixture")
    await journal.record_candidate(editing, candidate)
    await journal.transition(editing, "verifying")
    await journal.transition(await journal.claim("fixture"), "publishing")
    publishing = await journal.claim("fixture")
    pr = {"number": 1, "url": "https://github.com/fixture/theo/pull/1", "sha": candidate.commit}
    await journal.effect_intent(publishing, "pull_request", {})
    await journal.effect_receipt(publishing, "pull_request", pr)
    await journal.transition(publishing, "awaiting_ci")
    await journal.transition(await journal.claim("fixture"), "merging")
    if merge_state == "unknown":
        merging = await journal.claim("fixture")
        await journal.effect_intent(merging, "merge", {"sha": candidate.commit, "number": 1})
        await journal.effect_receipt(merging, "merge", None)
        await journal.transition(merging, "merging")
    mutations = []

    async def api(method, path, body=None):
        if method != "GET":
            mutations.append((method, path))
            raise AssertionError("No new merge request should be sent")
        assert path == "/pulls/1"
        return {
            "merged": merge_state == "already_merged",
            "head": {"sha": candidate.commit},
            "merge_commit_sha": "9" * 40,
        }

    async def ref(_branch):
        return "8" * 40

    monkeypatch.setattr(controller.github, "api", api)
    monkeypatch.setattr(controller.github, "ref", ref)
    try:
        assert await controller.tick()
        state = await journal.one("SELECT * FROM changes")
        iteration = await journal.current_round(accepted["id"])
        if merge_state == "base_advanced":
            assert (state["stage"], iteration["revision"], iteration["base_commit"]) == (
                "preparing",
                2,
                "8" * 40,
            )
            assert iteration["coding_job_id"] is None
        elif merge_state == "unknown":
            assert (state["stage"], state["status"], iteration["revision"]) == (
                "merging",
                "uncertain",
                1,
            )
        else:
            assert state["stage"] == "packaging" and iteration["revision"] == 1
            assert (await journal.receipt(accepted["id"], "merge"))["sha"] == "9" * 40
        assert mutations == []
    finally:
        await controller.github.close()
        await journal.close()


async def test_revision_push_uses_exact_previous_receipt_and_refuses_foreign_head(
    configured, monkeypatch
):
    config, policy = configured
    github = GitHub(config, policy)
    from theo.maintenance.contracts import CandidateIdentity

    candidate = CandidateIdentity(
        change_id="change",
        revision=2,
        base_commit="1" * 40,
        commit="2" * 40,
        tree="3" * 40,
        snapshot_sha256="4" * 64,
        lock_sha256="5" * 64,
    )
    remote = "6" * 40
    commands = []

    async def identity():
        return None

    async def authenticate():
        github.token = "synthetic"

    async def ref(_branch):
        return remote

    async def git_call(_path, *args, **kwargs):
        nonlocal remote
        commands.append(args)
        remote = candidate.commit
        return "ok"

    monkeypatch.setattr(github, "identity", identity)
    monkeypatch.setattr(github, "authenticate", authenticate)
    monkeypatch.setattr(github, "ref", ref)
    monkeypatch.setattr("theo.maintenance.github.git", git_call)
    try:
        with pytest.raises(Conflict, match="different commit"):
            await github.push(config.root, candidate, "theo/change", previous="7" * 40)
        assert not commands
        assert (await github.push(config.root, candidate, "theo/change", previous=remote))[
            "sha"
        ] == candidate.commit
        assert "--force-with-lease=refs/heads/theo/change:" + "6" * 40 in commands[0]
        assert (await github.push(config.root, candidate, "theo/change", previous="6" * 40))[
            "sha"
        ] == candidate.commit
        assert len(commands) == 1
    finally:
        await github.close()


async def test_recovery_preserves_model_pause_without_creating_inference(
    db, configured, monkeypatch
):
    config, _ = configured
    host = Host(
        HostConfig(
            root=db.root,
            state_root=db.root.parent / "recovery",
            bundles=config.root / "bundles",
            socket=config.host_socket,
            token_file=config.host_token_file,
        )
    )
    await host.db.close()
    host.db = db
    host.save(stage="recovery_check", deadline=db.clock() + 100, change_id="change")
    await db.set_control("owner", "models_paused", "true")
    await db.set_control("owner", "maintenance_draining", "true")

    async def healthy():
        return True

    async def forbidden():
        raise AssertionError("Rollback must not request a native model")

    monkeypatch.setattr(host, "healthy", healthy)
    monkeypatch.setattr(host, "canary", forbidden)
    await host.tick()
    assert host.state["stage"] == "rolled_back"
    assert host.state["recovery_verification"] == "deterministic"
    assert await db.control("owner", "models_paused") == "true"
    assert await db.control("owner", "maintenance_draining") == "false"
    assert not await db.read("SELECT * FROM jobs")


async def test_model_pause_parks_activation_canary_without_admitting_a_job(
    db, configured, monkeypatch
):
    from theo.maintenance.host import CanaryUnavailable

    config, _ = configured
    host = Host(
        HostConfig(
            root=db.root,
            state_root=db.root.parent / "activation",
            bundles=config.root / "bundles",
            socket=config.host_socket,
            token_file=config.host_token_file,
        )
    )
    await host.db.close()
    host.db = db
    host.save(stage="checking", deadline=db.clock() + 100, change_id="change")
    await db.set_control("owner", "models_paused", "true")

    async def healthy():
        return True

    monkeypatch.setattr(host, "healthy", healthy)
    with pytest.raises(CanaryUnavailable, match="models_paused"):
        await host.canary()
    assert not await db.read("SELECT * FROM jobs")
