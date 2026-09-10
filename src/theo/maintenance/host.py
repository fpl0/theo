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
import pwd
import signal
import sys
from pathlib import Path
from typing import cast

import psutil
from pydantic import Field, model_validator

from theo.backends.process import stop_process
from theo.config import Settings, load_settings
from theo.domain import Conflict, Denied, Json, StrictModel, encode
from theo.execution.processes import terminate_tree
from theo.maintenance.bundles import atomic_select, verify
from theo.maintenance.configuration import read_operator_file, require_root_parents
from theo.maintenance.core_access import operation as core_operation
from theo.maintenance.policy import load_policy
from theo.maintenance.rpc import serve
from theo.storage import Database


class CanaryUnavailable(Conflict):
    """External eligibility is unavailable; running code is not a proven regression."""


class HostConfig(StrictModel):
    controller_uid: int = Field(default_factory=os.geteuid, ge=0)
    core_uid: int = Field(default_factory=os.geteuid, ge=0)
    core_gid: int = Field(default_factory=os.getegid, ge=0)
    root: Path
    state_root: Path
    selection: Path | None = None
    telegram_token_file: Path | None = None
    bundles: Path
    socket: Path
    token_file: Path
    policy: Path | None = None
    drain_seconds: int = Field(default=120, ge=10, le=600)
    startup_seconds: int = Field(default=120, ge=10, le=600)
    probation_seconds: int = Field(default=600, ge=60, le=3600)

    @model_validator(mode="after")
    def paths(self) -> HostConfig:
        if any(
            not path.is_absolute()
            for path in (
                self.root,
                self.state_root,
                self.bundles,
                self.socket,
                self.token_file,
                self.policy,
                self.selection,
                self.telegram_token_file,
            )
            if path is not None
        ):
            raise ValueError("Supervisor installation paths must be absolute")
        return self


class Host:
    def __init__(self, config: HostConfig):
        self.config = config
        self.path = config.state_root / "activation.json"
        self.state: Json = (
            json.loads(self.path.read_text()) if self.path.exists() else {"stage": "idle"}
        )
        self.db = Database(config.root)
        self.settings = (
            Settings(owner_id=load_policy(config.policy, expected_uid=0).owner_id)
            if os.geteuid() == 0 and config.policy
            else load_settings(config.root)
        )
        self.child: asyncio.subprocess.Process | None = None
        self.mutex = asyncio.Lock()

    async def core(self, name: str, body: Json | None = None) -> Json:
        """Access mutable core storage with the core UID, never root authority."""
        if os.geteuid() != 0:
            # In-process fixtures retain their injected clock and real SQLite.
            # Production run() requires the privileged, separated supervisor.
            return await core_operation(self.db, self.settings, name, body or {})
        environment = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "THEO_TEST_OFFLINE": os.environ.get("THEO_TEST_OFFLINE", "0"),
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            "-m",
            "theo.maintenance.core_access",
            "--root",
            str(self.config.root),
            "--owner",
            self.settings.owner_id,
            name,
            user=self.config.core_uid,
            group=self.config.core_gid,
            extra_groups=[],
            env=environment,
            cwd="/",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        assert process.stdin and process.stdout
        try:
            async with asyncio.timeout(900 if name == "backup" else 30):
                process.stdin.write(encode(body or {}).encode())
                await process.stdin.drain()
                process.stdin.close()
                output = bytearray()
                while chunk := await process.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > 65536:
                        raise Denied("Core database helper response exceeded its bound")
                await process.wait()
            if process.returncode:
                raise Denied("Core database helper failed under its restricted service identity")
            response = json.loads(output)
            if not isinstance(response, dict):
                raise Denied("Invalid core database response")
            return cast(Json, response)
        finally:
            await stop_process(process)

    async def control(self, key: str) -> str | None:
        return (await self.core("control", {"key": key}))["value"]

    async def draining(self, paused: bool) -> None:
        await self.core("draining", {"paused": paused})

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
        return self.config.selection or self.config.root / "releases/current"

    def active(self) -> Path:
        if not self.pointer.is_symlink():
            raise Denied("Install a recoverable baseline bundle before enabling self-deployment")
        selected = self.pointer.resolve(strict=True)
        if selected.parent != self.config.bundles.resolve(strict=True):
            raise Denied("The selected runtime must be an accepted installation bundle")
        return selected

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
        account = pwd.getpwuid(self.config.core_uid)
        env = {
            "HOME": account.pw_dir,
            "USER": account.pw_name,
            "LOGNAME": account.pw_name,
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "THEO_BUNDLE_ID": bundle.bundle_id,
            "THEO_SELECTED_BUNDLE": str(selected),
            "PATH": str(selected / "native/bin")
            + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        }
        # Explicit application settings may cross the supervisor boundary;
        # control credentials and interpreter startup customization cannot.
        for key in (
            "THEO_TELEMETRY_ENABLED",
            "THEO_OTLP_ENDPOINT",
            "THEO_ENVIRONMENT",
            "THEO_TRACE_SAMPLE_RATIO",
            "THEO_TEST_OFFLINE",
        ):
            if key in os.environ:
                env[key] = os.environ[key]
        if self.config.telegram_token_file:
            env["THEO_TELEGRAM_TOKEN"] = read_operator_file(
                self.config.telegram_token_file, private=True
            ).strip()
        self.child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            "-m",
            "theo.maintenance.launcher",
            "--state-root",
            str(self.config.state_root),
            "--data-root",
            str(self.config.root),
            "--python",
            str(selected / bundle.core_python),
            "--uid",
            str(self.config.core_uid),
            "--gid",
            str(self.config.core_gid),
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
        if (await self.core("daemon_stopped"))["stopped"] is not True:
            raise Conflict("Core daemon lock is still held; activation cannot proceed")
        self.save(core_pid=None, core_birth=None)

    def deployment_allowed(self) -> None:
        if self.config.policy is None:
            raise Denied("Configure protected deployment policy for the host service")
        policy = load_policy(self.config.policy, expected_uid=0)
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
                    await self.control("deployments_paused") == "true"
                    or await self.control("quarantined") == "true"
                ):
                    raise Denied("Deployments are paused or installation is quarantined")
                if await self.control("models_paused") == "true":
                    raise Denied("Native activation canary requires models to be available")
                if Path(body["bundle_id"]).name != body["bundle_id"]:
                    raise Denied("Invalid bundle identity")
                self.deployment_allowed()
                target = self.config.bundles / body["bundle_id"]
                bundle = verify(target, body["fingerprint"])
                previous = self.active()
                old = verify(previous)
                schema = (await self.core("schema"))["version"]
                if not (
                    schema is not None
                    and bundle.schema_min <= schema <= bundle.schema_max
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
                await self.draining(True)
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
                    await self.core(
                        "retry_canary",
                        {
                            "available_at": self.db.clock(),
                            "deadline": self.db.clock() + self.config.startup_seconds,
                            "job_id": self.state.get("canary_job"),
                        },
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
            heartbeat = await self.core("heartbeat")
            age = self.db.clock() - heartbeat["timestamp"]
            return heartbeat["pid"] == process.pid and 0 <= age < 90
        except OSError, ValueError, TypeError, KeyError:
            return False

    async def deterministic_health(self) -> bool:
        """Verify a running core and its canonical database without admitting inference."""
        if not await self.healthy():
            return False
        if (await self.core("integrity"))["ok"] is not True:
            raise Denied("Canonical database integrity check failed")
        return True

    async def canary(self) -> bool:
        if not await self.deterministic_health():
            return False
        if await self.control("models_paused") == "true":
            raise CanaryUnavailable("models_paused")
        if not self.state.get("canary_job"):
            result = await self.core(
                "canary_create",
                {
                    "change_id": self.state["change_id"],
                    "bundle_id": self.active().name,
                    "deadline": self.db.clock() + self.config.startup_seconds,
                },
            )
            self.save(canary_job=result["job_id"])
            return False
        result = await self.core("canary_status", {"job_id": self.state["canary_job"]})
        if result["status"] in ("waiting_for_auth", "waiting_for_quota"):
            raise CanaryUnavailable(result["status"])
        return result["tool_succeeded"] is True

    async def tick(self) -> None:
        async with self.mutex:
            try:
                await self.advance()
            except CanaryUnavailable as exc:
                await self.draining(False)
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
                    await self.draining(False)
                    self.save(stage="failed", error=type(exc).__name__)

    async def advance(self) -> None:
        stage = self.state["stage"]
        timestamp = self.db.clock()
        if self.state.get("verification_blocker"):
            if not await self.healthy():
                self.save(stage="recovering", verification_blocker=None)
            return
        if stage in ("idle", "deployed", "rolled_back", "cancelled", "failed"):
            if (await self.core("paused"))["paused"] is True:
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
            await self.draining(False)
            self.save(stage="cancelled")
            return
        if stage == "draining":
            self.deployment_allowed()
            if timestamp > self.state["deadline"]:
                raise Denied("Drain deadline expired")
            if await self.control("deployments_paused") == "true":
                raise Denied("Owner paused deployment during drain")
            if (await self.core("quiescent"))["ready"] is not True:
                return
            await self.stop()
            snapshot = await self.core("backup")
            self.save(stage="activating", snapshot=snapshot["path"])
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
                await self.draining(False)
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
            await self.draining(True)
            await self.stop()
            previous = Path(self.state["previous"])
            bundle = verify(previous, self.state["previous_fingerprint"])
            schema = (await self.core("schema"))["version"]
            if schema is None or not bundle.schema_min <= schema <= bundle.schema_max:
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
                await self.draining(False)
                self.save(
                    stage="rolled_back",
                    completed_at=timestamp,
                    recovery_verification="deterministic",
                )


async def run(config: HostConfig) -> None:
    if os.geteuid() != 0:
        raise Denied("Install the independent supervisor as its protected root service")
    if (
        config.core_uid == 0
        or config.core_gid == 0
        or config.controller_uid in (0, config.core_uid)
        or pwd.getpwuid(config.controller_uid).pw_gid in (0, config.core_gid)
        or config.selection is None
        or not config.policy
    ):
        raise Denied("A privileged supervisor requires a non-root core and protected selection")
    read_operator_file(config.policy)
    for path in (config.state_root, config.selection.parent, config.socket.parent):
        require_root_parents(path)
        if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
            raise Denied("Supervisor authority directories must be root-owned and protected")
        if path.resolve().is_relative_to(config.root.resolve()):
            raise Denied("Supervisor authority cannot be stored in the core's writable root")
    if config.state_root.stat().st_mode & 0o077:
        raise Denied("Supervisor process records must be private to root")
    if config.telegram_token_file:
        read_operator_file(config.telegram_token_file, private=True)
    read_operator_file(config.token_file)
    if (
        config.token_file.stat().st_gid != pwd.getpwuid(config.controller_uid).pw_gid
        or config.token_file.stat().st_mode & 0o007
    ):
        raise Denied("Host RPC credentials must be readable only by root and the controller")
    require_root_parents(config.bundles)
    if (
        config.bundles.is_symlink()
        or config.bundles.stat().st_uid != config.controller_uid
        or config.bundles.stat().st_mode & 0o022
    ):
        raise Denied("Accepted bundles must be protected controller-owned storage")
    os.umask(0o077)
    with (config.state_root / "host.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        host = Host(config)
        stop = asyncio.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(signum, stop.set)
        server = await serve(
            config.socket, config.token_file, host.rpc, expected_uid=config.controller_uid
        )
        os.chown(config.socket, -1, pwd.getpwuid(config.controller_uid).pw_gid)
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
    asyncio.run(run(HostConfig.model_validate_json(read_operator_file(args.config))))


if __name__ == "__main__":
    main()
