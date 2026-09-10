"""Independent core supervisor and durable application activation/recovery.

This pinned service outlives the application and the maintenance controller. Its
narrow local protocol accepts bundle identities, never paths or shell commands.
"""

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import signal
import sys
from pathlib import Path

import psutil
from pydantic import Field

from theo.config import load_settings
from theo.domain import Conflict, Denied, Json, StrictModel, encode
from theo.execution.processes import terminate_tree
from theo.maintenance.bundles import atomic_select, verify
from theo.maintenance.configuration import read_protected
from theo.maintenance.policy import load_policy
from theo.maintenance.rpc import serve
from theo.operations.backups import backup_create
from theo.storage import Database
from theo.work.jobs import Jobs


class CanaryUnavailable(Conflict):
    """External eligibility is unavailable; running code is not a proven regression."""


class HostConfig(StrictModel):
    controller_uid: int = Field(default_factory=os.geteuid, ge=0)
    root: Path
    state_root: Path
    bundles: Path
    socket: Path
    token_file: Path
    policy: Path | None = None
    drain_seconds: int = Field(default=120, ge=10, le=600)
    startup_seconds: int = Field(default=120, ge=10, le=600)
    probation_seconds: int = Field(default=600, ge=60, le=3600)


class Host:
    def __init__(self, config: HostConfig):
        self.config = config
        self.path = config.state_root / "activation.json"
        self.state: Json = (
            json.loads(self.path.read_text()) if self.path.exists() else {"stage": "idle"}
        )
        self.db = Database(config.root)
        self.settings = load_settings(config.root)
        self.child: asyncio.subprocess.Process | None = None
        self.mutex = asyncio.Lock()

    def save(self, **values: object) -> None:
        self.state.update(values)
        self.config.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            stream.write(encode(self.state))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @property
    def pointer(self) -> Path:
        return self.config.root / "releases/current"

    def active(self) -> Path:
        if not self.pointer.is_symlink():
            raise Denied("Install a recoverable baseline bundle before enabling self-deployment")
        return self.pointer.resolve(strict=True)

    def process(self) -> psutil.Process | None:
        recorded = self.config.state_root / "core-process.json"
        if recorded.exists():
            identity = json.loads(recorded.read_text())
            if identity.get("root") != str(self.config.root):
                raise Denied("Core process identity belongs to another installation")
            pid, birth = identity.get("pid"), identity.get("birth")
        else:
            pid, birth = self.state.get("core_pid"), self.state.get("core_birth")
        if not pid or not birth:
            return None
        try:
            process = psutil.Process(pid)
            return (
                process
                if process.create_time() == birth and process.status() != psutil.STATUS_ZOMBIE
                else None
            )
        except psutil.NoSuchProcess:
            return None

    async def start(self) -> None:
        if self.process():
            return
        selected = self.active()
        bundle = verify(selected)
        env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "THEO_BUNDLE_ID": bundle.bundle_id,
            "PATH": str(selected / "native/bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
        }
        # Installation credentials are not forwarded to the application.
        for key in tuple(env):
            if key.startswith(("GITHUB_", "GH_", "THEO_GITHUB_", "THEO_MAINTENANCE_")):
                del env[key]
        self.child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "theo.maintenance.launcher",
            "--state-root",
            str(self.config.state_root),
            "--data-root",
            str(self.config.root),
            "--python",
            str(selected / bundle.core_python),
            env=env,
            start_new_session=True,
        )
        birth = psutil.Process(self.child.pid).create_time()
        self.save(core_pid=self.child.pid, core_birth=birth, started_at=self.db.clock())

    async def stop(self) -> None:
        process = self.process()
        if process:
            await asyncio.to_thread(terminate_tree, process.pid, created_at=process.create_time())
        if self.child:
            await self.child.wait()
            self.child = None
        with (self.config.root / "daemon.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Conflict(
                    "Core daemon lock is still held; activation cannot proceed"
                ) from None
        self.save(core_pid=None, core_birth=None)

    def deployment_allowed(self) -> None:
        if self.config.policy is None:
            raise Denied("Configure protected deployment policy for the host service")
        policy = load_policy(self.config.policy)
        if (
            not policy.enabled
            or not policy.allow_deploy
            or policy.owner_id != self.settings.owner_id
        ):
            raise Denied("Deployment authority is disabled or no longer matches this owner")

    async def rpc(self, operation: str, body: Json) -> Json:
        async with self.mutex:
            if operation == "status" and not body:
                return await self.status()
            if operation == "activate" and set(body) == {"change_id", "bundle_id", "fingerprint"}:
                if self.state.get("change_id") == body["change_id"]:
                    if (
                        self.state.get("bundle_id") != body["bundle_id"]
                        or self.state.get("fingerprint") != body["fingerprint"]
                    ):
                        raise Conflict("Activation identity binds another bundle")
                    return await self.status()
                if self.state["stage"] not in (
                    "idle",
                    "deployed",
                    "rolled_back",
                    "failed",
                    "cancelled",
                ):
                    raise Conflict("An activation or probation period is already in progress")
                if self.state.get("circuit_open"):
                    raise Denied("Recovery circuit is open; operator repair is required")
                if (
                    await self.db.control(self.settings.owner_id, "deployments_paused") == "true"
                    or await self.db.control(self.settings.owner_id, "quarantined") == "true"
                ):
                    raise Denied("Deployments are paused or installation is quarantined")
                if await self.db.control(self.settings.owner_id, "models_paused") == "true":
                    raise Denied("Native activation canary requires models to be available")
                if Path(body["bundle_id"]).name != body["bundle_id"]:
                    raise Denied("Invalid bundle identity")
                self.deployment_allowed()
                target = self.config.bundles / body["bundle_id"]
                bundle = verify(target, body["fingerprint"])
                previous = self.active()
                old = verify(previous)
                schema = await self.db.one("SELECT max(version) n FROM schema_migrations")
                assert schema
                if not (
                    bundle.schema_min <= schema["n"] <= bundle.schema_max
                    and old.schema_min <= bundle.schema_max <= old.schema_max
                ):
                    raise Denied(
                        "No verified old/new schema compatibility; stage an expand/contract recovery baseline first"
                    )
                if target == previous:
                    raise Conflict("Candidate is already selected")
                self.save(
                    **body,
                    previous=str(previous),
                    previous_fingerprint=old.fingerprint,
                    stage="draining",
                    deadline=self.db.clock() + self.config.drain_seconds,
                    cancellation=False,
                    rollback=False,
                    error=None,
                    canary_job=None,
                )
                await self.db.set_control(self.settings.owner_id, "maintenance_draining", "true")
                return await self.status()
            if operation == "cancel" and set(body) == {"change_id"}:
                if self.state.get("change_id") != body["change_id"]:
                    raise Denied("Unknown activation")
                self.save(cancellation=True, verification_blocker=None)
                return await self.status()
            if operation == "rollback" and set(body) == {"change_id"}:
                if (
                    self.state.get("change_id") != body["change_id"]
                    or self.state["stage"] != "deployed"
                ):
                    if self.state.get("change_id") == body["change_id"] and self.state["stage"] in (
                        "recovering",
                        "recovery_check",
                        "rolled_back",
                    ):
                        return await self.status()
                    raise Denied("Rollback requires the currently active recorded deployment")
                self.save(rollback=True, stage="recovering")
                return await self.status()
            if operation == "retry_verification" and set(body) == {"change_id"}:
                if self.state.get("change_id") != body["change_id"]:
                    raise Denied("Unknown activation")
                if self.state.get("verification_blocker"):
                    await self.db.execute(
                        "UPDATE jobs SET status='queued',generation=generation+1,available_at=?,deadline=? WHERE id=? AND status IN ('waiting_for_auth','waiting_for_quota')",
                        (
                            self.db.clock(),
                            self.db.clock() + self.config.startup_seconds,
                            self.state.get("canary_job"),
                        ),
                    )
                    self.save(
                        verification_blocker=None,
                        deadline=self.db.clock() + self.config.startup_seconds,
                    )
                return await self.status()
            raise Denied("Unsupported host operation")

    async def status(self) -> Json:
        return {**self.state, "active": self.active().name, "running": self.process() is not None}

    async def healthy(self) -> bool:
        process = self.process()
        if not process:
            return False
        try:
            heartbeat = json.loads((self.config.root / "heartbeat.json").read_text())
            age = self.db.clock() - heartbeat["timestamp"]
            return heartbeat["pid"] == process.pid and 0 <= age < 90
        except OSError, ValueError, KeyError:
            return False

    async def deterministic_health(self) -> bool:
        """Verify a running core and its canonical database without admitting inference."""
        if not await self.healthy():
            return False
        integrity = await self.db.one("PRAGMA integrity_check")
        if not integrity or list(integrity.values()) != ["ok"]:
            raise Denied("Canonical database integrity check failed")
        return True

    async def canary(self) -> bool:
        if not await self.deterministic_health():
            return False
        if await self.db.control(self.settings.owner_id, "models_paused") == "true":
            raise CanaryUnavailable("models_paused")
        if not self.state.get("canary_job"):
            conversation = await self.db.conversation(
                self.settings.owner_id, "local", "maintenance-canary:" + self.state["change_id"]
            )
            job = await Jobs(self.db, self.settings.owner_id).enqueue(
                conversation,
                "maintenance_canary",
                {
                    "text": "Synthetic deployment check. Call get_status exactly once and then say checked. Do not save memories or send messages."
                },
                "activation-canary:" + self.state["change_id"] + ":" + self.active().name,
                lane="interactive",
                deadline=self.db.clock() + self.config.startup_seconds,
                origin="system",
            )
            self.save(canary_job=job)
            return False
        job = await self.db.one("SELECT status FROM jobs WHERE id=?", (self.state["canary_job"],))
        receipt = await self.db.one(
            "SELECT m.content FROM messages m JOIN runs r ON r.id=m.run_id JOIN jobs j ON j.id=r.job_id AND j.generation=r.generation WHERE r.job_id=? AND m.source='tool:get_status' ORDER BY m.created_at DESC LIMIT 1",
            (self.state["canary_job"],),
        )
        if job and job["status"] in ("waiting_for_auth", "waiting_for_quota"):
            raise CanaryUnavailable(job["status"])
        if not job or job["status"] != "completed" or not receipt:
            return False
        result = json.loads(receipt["content"])["result"]
        return (
            result["status"] not in {"failed", "denied", "invalid", "uncertain"}
            and result.get("data") is not None
        )

    async def tick(self) -> None:
        async with self.mutex:
            try:
                await self.advance()
            except CanaryUnavailable as exc:
                await self.db.set_control(self.settings.owner_id, "maintenance_draining", "false")
                self.save(verification_blocker=str(exc))
            except Exception as exc:
                stage = self.state["stage"]
                if stage in ("recovering", "recovery_check"):
                    self.save(
                        stage="failed", circuit_open=True, error="recovery_" + type(exc).__name__
                    )
                    await self.stop()
                elif stage in ("activating", "checking", "observing"):
                    self.save(stage="recovering", error=type(exc).__name__)
                else:
                    await self.db.set_control(
                        self.settings.owner_id, "maintenance_draining", "false"
                    )
                    self.save(stage="failed", error=type(exc).__name__)

    async def advance(self) -> None:
        stage = self.state["stage"]
        timestamp = self.db.clock()
        if self.state.get("verification_blocker"):
            if not await self.healthy():
                self.save(stage="recovering", verification_blocker=None)
            return
        if stage in ("idle", "deployed", "rolled_back", "cancelled", "failed"):
            if (self.config.root / "maintenance.pause").exists():
                await self.stop()
                return
            if (
                self.process()
                and timestamp - self.state.get("started_at", timestamp) > 90
                and not await self.healthy()
            ):
                await self.stop()
            if not self.state.get("circuit_open") and not self.process():
                failures = [t for t in self.state.get("failures", []) if timestamp - t < 3600]
                if len(failures) >= 5:
                    self.save(circuit_open=True, error="restart_limit")
                    return
                if timestamp >= self.state.get("restart_after", 0):
                    self.save(failures=[*failures, timestamp], restart_after=timestamp + 30)
                    await self.start()
            return
        if self.state.get("cancellation") and stage == "draining":
            await self.db.set_control(self.settings.owner_id, "maintenance_draining", "false")
            self.save(stage="cancelled")
            return
        if stage == "draining":
            self.deployment_allowed()
            if timestamp > self.state["deadline"]:
                raise Denied("Drain deadline expired")
            if await self.db.control(self.settings.owner_id, "deployments_paused") == "true":
                raise Denied("Owner paused deployment during drain")
            pending = await self.db.one(
                "SELECT (SELECT count(*) FROM jobs WHERE status='running')+(SELECT count(*) FROM outbox WHERE status='executing')+(SELECT count(*) FROM actions WHERE status='executing') n"
            )
            if pending and pending["n"]:
                return
            await self.stop()
            snapshot = await backup_create(self.db, self.settings, release_snapshot=True)
            self.save(stage="activating", snapshot=str(snapshot))
            return
        if stage == "activating":
            if self.active() == Path(self.state["previous"]):
                self.deployment_allowed()
            target = self.config.bundles / self.state["bundle_id"]
            verify(target, self.state["fingerprint"])
            if self.active() != target.resolve():
                atomic_select(self.pointer, target, Path(self.state["previous"]))
            await self.start()
            self.save(stage="checking", deadline=timestamp + self.config.startup_seconds)
            return
        if stage in ("checking", "observing") and self.state.get("cancellation"):
            self.save(stage="recovering")
            return
        if stage == "checking":
            if timestamp > self.state["deadline"]:
                raise Denied("Startup or native tool canary deadline expired")
            if await self.canary():
                # Draining is maintenance-owned; no owner pause is overwritten.
                await self.db.set_control(self.settings.owner_id, "maintenance_draining", "false")
                self.save(stage="observing", deadline=timestamp + self.config.probation_seconds)
            return
        if stage == "observing":
            if not await self.healthy():
                raise Denied("Candidate lost its process or heartbeat during probation")
            if timestamp >= self.state["deadline"]:
                verify(self.active(), self.state["fingerprint"])
                self.save(stage="deployed", completed_at=timestamp)
            return
        if stage == "recovering":
            await self.db.set_control(self.settings.owner_id, "maintenance_draining", "true")
            await self.stop()
            previous = Path(self.state["previous"])
            bundle = verify(previous, self.state["previous_fingerprint"])
            schema = await self.db.one("SELECT max(version) n FROM schema_migrations")
            if not schema or not bundle.schema_min <= schema["n"] <= bundle.schema_max:
                raise Denied("Previous bundle cannot read and write the current schema")
            active = self.active()
            if active != previous:
                atomic_select(self.pointer, previous, self.config.bundles / self.state["bundle_id"])
            self.save(canary_job=None)
            await self.start()
            self.save(stage="recovery_check", deadline=timestamp + self.config.startup_seconds)
            return
        if stage == "recovery_check":
            if timestamp > self.state["deadline"]:
                raise Denied("Recovery health verification expired")
            # Recovery selects the already verified previous bundle. Requiring
            # new inference here would strand an owner's explicit model pause.
            if await self.deterministic_health():
                await self.db.set_control(self.settings.owner_id, "maintenance_draining", "false")
                self.save(
                    stage="rolled_back",
                    completed_at=timestamp,
                    recovery_verification="deterministic",
                )


async def run(config: HostConfig) -> None:
    config.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (config.state_root / "host.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        host = Host(config)
        stop = asyncio.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(signum, stop.set)
        server = await serve(
            config.socket, config.token_file, host.rpc, expected_uid=config.controller_uid
        )
        try:
            async with server:
                while not stop.is_set():
                    await host.tick()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), 1)
        finally:
            await host.stop()
            await host.db.close()
            config.socket.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(HostConfig.model_validate_json(read_protected(args.config))))


if __name__ == "__main__":
    main()
