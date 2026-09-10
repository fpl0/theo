"""Manage a disposable, disconnected Mac builder and its authenticated guest stream.

Only pinned host programs run here. Candidate inputs are regular copied files;
no host directory is mounted in the VM. The guest owns no host credentials.
"""

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import uuid
from pathlib import Path
from typing import BinaryIO, cast

import psutil

from theo.backends.process import stop_process
from theo.domain import Denied, Json
from theo.execution.processes import terminate_tree
from theo.maintenance.dependencies import wheel_requests
from theo.maintenance.source import IGNORED, files_digest, read_source
from theo.maintenance.vm_config import VmSettings

GUEST_ROOT = "/private/var/theo-builder"
GUEST_PYTHON = GUEST_ROOT + "/tools/python/bin/python3"
GUEST_RUNNER = GUEST_ROOT + "/tools/vm_guest.py"
GUEST_SOURCE = GUEST_ROOT + "/work/source"


def checksum(path: Path, *, maximum: int) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        raise Denied("VM inputs must be bounded regular files without links")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def alive(record: Json, key: str = "pid") -> bool:
    try:
        process = psutil.Process(int(record[key]))
        return (
            abs(process.create_time() - float(record["birth" if key == "pid" else "child_birth"]))
            < 0.01
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False


async def bounded(stream: asyncio.StreamReader, maximum: int) -> bytes:
    output = bytearray()
    while chunk := await stream.read(65536):
        output.extend(chunk)
        if len(output) > maximum:
            raise Denied("VM control response exceeded its bound")
    return bytes(output)


def archive_files(path: Path, files: dict[str, tuple[bytes, int]]) -> None:
    with tarfile.open(path, "w") as archive:
        for name, (body, mode) in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.mode = mode
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))


def response(raw: bytes) -> Json:
    try:
        value = cast(object, json.loads(raw))
    except ValueError:
        raise Denied("Malformed VM control response") from None
    if not isinstance(value, dict):
        raise Denied("VM control response must be an object")
    return cast(Json, value)


class VmDriver:
    def __init__(self, settings: VmSettings):
        self.settings = settings
        self.name = "theo-build-" + uuid.uuid4().hex
        self.directory = settings.root / "jobs" / self.name
        self.registry = settings.root / "driver-process.json"
        self.auxiliary = settings.root / "tool-process.json"
        self.process: asyncio.subprocess.Process | None = None
        self.lock: BinaryIO | None = None
        self.created = False
        self.admitted = False

    @property
    def environment(self) -> dict[str, str]:
        return {
            "HOME": str(self.settings.root / "home"),
            "PATH": str(self.settings.root / "bin") + ":/usr/bin:/bin:/usr/sbin:/sbin",
            "TART_HOME": str(self.settings.tart_home),
            "TART_NO_AUTO_PRUNE": "1",
            "CI": "1",
            "DO_NOT_TRACK": "1",
        }

    async def host(
        self,
        argv: list[str],
        *,
        input_file: Path | None = None,
        timeout: int = 30,
        maximum: int = 256 * 1024,
    ) -> bytes:
        with contextlib.ExitStack() as stack:
            stream = stack.enter_context(input_file.open("rb")) if input_file else None
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                str(Path(__file__).with_name("worker_launcher.py")),
                str(self.auxiliary),
                str(timeout),
                *argv,
                env=self.environment,
                stdin=stream if stream else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            assert process.stdout and process.stderr
            try:
                async with asyncio.timeout(timeout + 5):
                    output, error, _ = await asyncio.gather(
                        bounded(process.stdout, maximum),
                        bounded(process.stderr, 256 * 1024),
                        process.wait(),
                    )
                if process.returncode:
                    self.directory.mkdir(parents=True, exist_ok=True)
                    (self.directory / "control-error.log").write_bytes(error)
                    raise Denied("VM control command failed; preserve its private error log")
                return output
            finally:
                await stop_process(process)

    async def guest(
        self,
        argv: list[str],
        *,
        input_file: Path | None = None,
        timeout: int = 30,
        maximum: int = 256 * 1024,
    ) -> bytes:
        return await self.host(
            [str(self.settings.tart), "exec", *(["-i"] if input_file else []), self.name, *argv],
            input_file=input_file,
            timeout=timeout,
            maximum=maximum,
        )

    def validate(self) -> None:
        config = self.settings
        if sys.platform != "darwin":
            raise Denied("The VM builder requires the qualified Mac host")
        for path in (config.root, config.tart_home):
            if (
                path.is_symlink()
                or path.stat().st_uid != os.geteuid()
                or path.stat().st_mode & 0o077
            ):
                raise Denied("VM state must be private to its controller identity")
        pins = (
            (config.tart, config.tart_sha256, 100 * 1024 * 1024),
            (config.python_archive, config.python_sha256, 200 * 1024 * 1024),
            (config.uv, config.uv_sha256, 100 * 1024 * 1024),
            (config.agent, config.agent_sha256, 100 * 1024 * 1024),
        )
        for path, expected, maximum in pins:
            if checksum(path, maximum=maximum) != expected:
                raise Denied("Pinned VM runtime input changed")
        base = config.tart_home / "vms" / config.base_name
        for name, expected in config.base_files.items():
            if checksum(base / name, maximum=60_000_000_000) != expected:
                raise Denied("Pinned VM base changed")
        disk_bytes = sum(
            path.stat().st_size for path in (config.tart_home / "vms").glob("*/disk.img")
        )
        if disk_bytes + (base / "disk.img").stat().st_size > config.max_virtual_disk_bytes:
            raise Denied("Retained VM disks exceed the installation budget")
        if shutil.disk_usage(config.tart_home).free < (
            config.min_free_disk_bytes
            + (base / "disk.img").stat().st_size
            + config.max_input_bytes
            + config.max_export_bytes
        ):
            raise Denied("Insufficient reserved disk space for a disposable builder")

    async def recover(self) -> None:
        if self.lock is None:
            raise Denied("VM recovery requires exclusive installation ownership")
        for registry in (self.registry, self.auxiliary):
            if not registry.exists():
                continue
            record = cast(Json, json.loads(registry.read_bytes()))
            for key, birth in (("child_pid", "child_birth"), ("pid", "birth")):
                if record.get(key) and record.get(birth):
                    await asyncio.to_thread(
                        terminate_tree, int(record[key]), created_at=float(record[birth])
                    )
            if alive(record) or (record.get("child_pid") and alive(record, "child_pid")):
                raise Denied("A previous VM process has not exited")

    def acquire(self) -> None:
        config = self.settings
        config.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = config.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise Denied("VM control storage must be private to its controller")
        descriptor = os.open(
            config.root / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        self.lock = os.fdopen(descriptor, "a+b")
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise Denied("VM ownership lock must be a private regular file")
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            # Contention grants no recovery authority over the active owner.
            self.lock.close()
            self.lock = None
            raise

    async def __aenter__(self) -> VmDriver:
        config = self.settings
        self.acquire()
        try:
            await self.recover()
            await asyncio.to_thread(self.validate)
            self.directory.mkdir(parents=True)
            for path in (config.root / "home", config.root / "bin"):
                path.mkdir(mode=0o700, exist_ok=True)
            # Resolve the packet sink through an otherwise empty private PATH.
            # Never pick the routable vendor Softnet program from the host PATH.
            import shlex

            helper = config.root / "bin/softnet"
            helper.write_text(
                "#!/bin/sh\nexec "
                + shlex.quote(sys.executable)
                + " -I "
                + shlex.quote(str(Path(__file__).with_name("packet_sink.py")))
                + ' "$@"\n'
            )
            helper.chmod(0o700)
            await self.host(
                ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(config.tart.parents[2])]
            )
            await self.host([str(config.tart), "clone", config.base_name, self.name], timeout=120)
            self.created = True
            self.identity()
            await self.host(
                [
                    str(config.tart),
                    "set",
                    self.name,
                    "--cpu",
                    str(config.cpus),
                    "--memory",
                    str(config.memory_mib),
                ]
            )
            limits = self.directory / "limits.json"
            limits.write_text(
                json.dumps(
                    {
                        "vm_name": self.name,
                        "timeout": config.timeout,
                        "max_host_memory_bytes": config.max_host_memory_bytes,
                        "min_free_disk_bytes": config.min_free_disk_bytes,
                        "disk_root": str(config.tart_home),
                        "auxiliary_registry": str(self.auxiliary),
                        "log": str(self.directory / "driver.log"),
                    }
                )
            )
            with (self.directory / "driver.log").open("wb") as log:
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("vm_watchdog.py")),
                    str(self.registry),
                    str(limits),
                    str(config.tart),
                    "run",
                    self.name,
                    "--no-graphics",
                    "--no-clipboard",
                    "--no-audio",
                    "--net-softnet",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env=self.environment,
                    start_new_session=True,
                )
            await self.ready()
            return self
        except BaseException:
            await self.close()
            raise

    async def ready(self, *, root: bool = False) -> None:
        deadline = asyncio.get_running_loop().time() + 180
        while asyncio.get_running_loop().time() < deadline:
            if self.process and self.process.returncode is not None:
                raise Denied("The VM watchdog stopped before guest readiness")
            try:
                output = await self.guest(["/usr/bin/id", "-u"], timeout=5)
                if not root or output.strip() == b"0":
                    return
            except Denied, TimeoutError:
                pass
            await asyncio.sleep(2)
        raise Denied("The guest agent did not become ready before its deadline")

    async def prepare(self, source: Path, wheels: Path) -> None:
        inputs = self.directory / "inputs"
        inputs.mkdir()
        files = await asyncio.to_thread(read_source, source)
        await asyncio.to_thread(archive_files, inputs / "source.tar", files)
        expected_wheels = wheel_requests(files["uv.lock"][0])
        with tarfile.open(inputs / "wheels.tar", "w") as archive:
            for wheel in expected_wheels:
                path = wheels / wheel["name"]
                if checksum(path, maximum=200 * 1024 * 1024) != wheel["sha256"]:
                    raise Denied("Staged wheel differs from the candidate lock")
                member = tarfile.TarInfo(path.name)
                member.mode = 0o644
                member.size = path.stat().st_size
                with path.open("rb") as stream:
                    archive.addfile(member, stream)
        config = self.settings
        for name, path in (
            ("python.tar.gz", config.python_archive),
            ("uv", config.uv),
            ("tart-guest-agent", config.agent),
            ("vm_guest.py", Path(__file__).with_name("vm_guest.py")),
            ("vm_bootstrap.py", Path(__file__).with_name("vm_bootstrap.py")),
            ("vm_bundle.py", Path(__file__).with_name("vm_bundle.py")),
            ("vm_exports.py", Path(__file__).with_name("vm_exports.py")),
            ("installed_check.py", Path(__file__).with_name("installed_check.txt")),
        ):
            shutil.copyfile(path, inputs / name)
        if sum(path.stat().st_size for path in inputs.iterdir()) > config.max_input_bytes:
            raise Denied("Prepared VM input exceeds its installation budget")
        (inputs / "manifest.json").write_text(
            json.dumps(
                {
                    "source_sha256": files_digest(files),
                    "source_ignored": sorted(IGNORED),
                    "source_files": {
                        name: [hashlib.sha256(body).hexdigest(), mode]
                        for name, (body, mode) in files.items()
                    },
                    "files": {
                        path.name: checksum(path, maximum=config.max_input_bytes)
                        for path in inputs.iterdir()
                    },
                }
            )
        )
        await self.guest(
            [
                "/usr/bin/sudo",
                "-n",
                "/bin/mkdir",
                "-p",
                GUEST_ROOT + "/inputs",
                GUEST_ROOT + "/tools",
            ]
        )
        for path in sorted(inputs.iterdir()):
            await self.guest(
                [
                    "/usr/bin/sudo",
                    "-n",
                    "/bin/sh",
                    "-c",
                    "umask 022; cat > " + GUEST_ROOT + "/inputs/" + path.name,
                ],
                input_file=path,
                timeout=180,
            )
        verify_python = (
            "import pathlib,hashlib; p=pathlib.Path('" + GUEST_ROOT + "/inputs/python.tar.gz'); "
            "assert hashlib.sha256(p.read_bytes()).hexdigest()==" + repr(config.python_sha256)
        )
        await self.guest(["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-c", verify_python])
        await self.guest(
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/bin/tar",
                "-xzf",
                GUEST_ROOT + "/inputs/python.tar.gz",
                "-C",
                GUEST_ROOT + "/tools",
            ]
        )
        await self.guest(
            [
                "/usr/bin/sudo",
                "-n",
                GUEST_PYTHON,
                "-I",
                GUEST_ROOT + "/inputs/vm_bootstrap.py",
                "agent",
            ]
        )
        await self.ready(root=True)
        # Keep the VM if preparation fails after starting source extraction.
        self.admitted = True
        self.identity()
        prepared = response(
            await self.guest(
                [GUEST_PYTHON, "-I", GUEST_ROOT + "/tools/vm_bootstrap.py", "prepare"], timeout=120
            )
        )
        if (
            prepared.get("source_sha256") != files_digest(files)
            or prepared.get("ready") is not True
        ):
            raise Denied("Guest preparation receipt does not match the accepted source")

    async def recipe(self, name: str, argv: list[str], cwd: str, timeout: int) -> Json:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", name):
            raise Denied("Invalid guest operation identity")
        request = {
            "operation_id": name,
            "argv": argv,
            "cwd": cwd,
            "timeout": timeout,
            "source_imports": False,
        }
        path = self.directory / "request.json"
        path.write_text(json.dumps(request))
        result = response(
            await self.guest(
                [GUEST_PYTHON, "-I", GUEST_RUNNER], input_file=path, timeout=timeout + 10
            )
        )
        expected = hashlib.sha256(
            json.dumps(
                {k: v for k, v in request.items() if k != "operation_id"}, sort_keys=True
            ).encode()
        ).hexdigest()
        if (
            result.get("request_sha256") != expected
            or result.get("operation_id") != name
            or result.get("descendants_stopped") is not True
            or type(result.get("guest_uid")) is not int
            or result.get("guest_uid") != 622
            or type(result.get("exit_code")) is not int
            or result.get("limit") not in (None, "cancelled", "timeout", "output")
        ):
            raise Denied("Guest recipe receipt does not bind this command")
        size = result.get("output_bytes")
        if type(size) is not int or not 0 <= size <= 4 * 1024 * 1024:
            raise Denied("Guest recipe log exceeds its bound")
        output = bytearray()
        while len(output) < size:
            chunk = response(
                await self.guest(
                    [
                        GUEST_PYTHON,
                        "-I",
                        GUEST_RUNNER,
                        "--read-result",
                        name,
                        "--offset",
                        str(len(output)),
                    ]
                )
            )
            data = base64.b64decode(chunk["data_base64"], validate=True)
            if (
                chunk.get("offset") != len(output)
                or chunk.get("operation_id") != name
                or chunk.get("output_bytes") != size
                or chunk.get("output_sha256") != result.get("output_sha256")
                or not 0 < len(data) <= min(32768, size - len(output))
                or hashlib.sha256(data).hexdigest() != chunk.get("chunk_sha256")
            ):
                raise Denied("Guest log chunk failed its integrity check")
            output.extend(data)
        if hashlib.sha256(output).hexdigest() != result.get("output_sha256"):
            raise Denied("Guest log does not match the completed receipt")
        (self.directory / (name + ".log")).write_bytes(output)
        return result

    def identity(self) -> None:
        (self.directory / "identity.json").write_text(
            json.dumps(
                {"name": self.name, "base": self.settings.base_name, "admitted": self.admitted}
            )
        )

    async def source_check(self, expected: str) -> None:
        result = response(
            await self.guest([GUEST_PYTHON, "-I", GUEST_ROOT + "/tools/vm_exports.py", "source"])
        )
        if result.get("source_sha256") != expected:
            raise Denied("The guest's source no longer matches the accepted candidate")

    async def seal_bundle(self, expected_source: str) -> Json:
        helper = [GUEST_PYTHON, "-I", GUEST_ROOT + "/tools/vm_exports.py"]
        receipt = response(await self.guest([*helper, "seal"], timeout=300))
        size = receipt.get("archive_bytes")
        if (
            type(size) is not int
            or not 0 < size <= self.settings.max_export_bytes
            or receipt.get("source_sha256") != expected_source
            or not isinstance(receipt.get("archive_sha256"), str)
        ):
            raise Denied("Sealed guest bundle exceeds its size or source boundary")
        (self.directory / "export-seal.json").write_text(json.dumps(receipt))
        return receipt

    async def export_bundle(self, expected_source: str) -> tuple[Path, Json]:
        from theo.maintenance.vm_transfer import transfer

        receipt = await self.seal_bundle(expected_source)
        archive = self.directory / "bundle.tar.gz"
        helper = [GUEST_PYTHON, "-I", GUEST_ROOT + "/tools/vm_exports.py"]
        await transfer(
            [str(self.settings.tart), "exec", "-i", self.name, *helper, "stream"],
            self.environment,
            self.auxiliary,
            receipt,
            archive,
        )
        (self.directory / "export-receipt.json").write_text(json.dumps(receipt))
        return archive, receipt

    async def close(self, *, retain: bool = False) -> None:
        if self.lock is None:
            return
        try:
            if self.process:
                await stop_process(self.process)
                self.process = None
            await self.recover()
            if self.directory.exists() and self.registry.exists():
                state = response(self.registry.read_bytes())
                if state.get("vm_name") == self.name:
                    (self.directory / "process-receipt.json").write_text(json.dumps(state))
            if self.created and not retain:
                await self.host([str(self.settings.tart), "delete", self.name])
                self.created = False
        finally:
            if self.lock:
                self.lock.close()
                self.lock = None

    async def __aexit__(self, *arguments: object) -> None:
        await asyncio.shield(self.close(retain=bool(arguments[0]) and self.admitted))


async def reconcile(settings: VmSettings) -> None:
    """Stop a prior owned VM before the controller resumes its durable journal."""
    driver = VmDriver(settings)
    driver.acquire()
    await driver.close(retain=True)
