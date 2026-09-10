"""Trusted recipes executed in a networkless, workspace-only Mac sandbox.

Controller credentials and state are absent from both the process environment
and read grants. Receipts bind the immutable source and the installed recipe.
"""

import asyncio
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from theo.backends.process import stop_process
from theo.domain import Denied, Json, digest
from theo.maintenance.configuration import CheckRecipe, ControllerConfig
from theo.maintenance.contracts import CandidateIdentity
from theo.maintenance.source import read_source, source_digest, write_source


def output_file(path: Path, workspace: Path, *, limit: int = 256 * 1024 * 1024) -> Path:
    """Validate untrusted output before the controller reads or copies its bytes."""
    if not path.resolve(strict=True).is_relative_to(workspace.resolve()):
        raise Denied("Build output escapes its workspace")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise Denied("Build output must be a bounded regular file without links")
    return path


def sandbox(workspace: Path, reads: tuple[Path, ...], temporary: Path | None = None) -> str:
    def quote(path: Path) -> str:
        return json.dumps(str(path.resolve()))

    # Keep required OS process startup services while denying all non-allowlisted
    # filesystem content and every network connection, including local IPC.
    readable = (
        Path("/System"),
        Path("/usr/lib"),
        Path("/usr/share"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/Library/Apple"),
        Path("/etc/apache2/mime.types"),
        Path("/usr/share/zoneinfo"),
        Path("/Applications/Xcode.app/Contents/Developer"),
        Path("/Library/Developer/CommandLineTools"),
        workspace,
        *((temporary,) if temporary else ()),
        *reads,
    )
    profile = "(version 1)(allow default)(allow file-read-metadata)"
    profile += (
        "(deny file-read-data (require-all "
        + " ".join("(require-not (subpath " + quote(path) + "))" for path in readable)
        + ' (require-not (literal "/")) (require-not (literal "/dev/null")) (require-not (literal "/dev/urandom")) (require-not (literal "/dev/random"))))'
    )
    profile += (
        "(deny file-write* (require-all (require-not (subpath "
        + quote(workspace)
        + ")) "
        + ("(require-not (subpath " + quote(temporary) + ")) " if temporary else "")
        + '(require-not (literal "/dev/null"))))'
    )
    profile += "(deny network*)(deny mach-priv*)"
    # Offline protocol tests need Unix IPC and their own child process trees.
    # Paths remain local to this check; the controller and other jobs are outside
    # these filesystem and process-identity grants.
    for path in (workspace, *((temporary,) if temporary else ())):
        profile += (
            "(allow network-bind network-outbound network-inbound (subpath " + quote(path) + "))"
        )
    profile += "(deny process-info* signal (require-all (require-not (target self)) (require-not (target children)) (require-not (target same-sandbox))))"
    profile += '(deny process-exec (literal "/bin/launchctl") (literal "/usr/bin/security") (literal "/usr/bin/sudo") (literal "/usr/bin/su") (literal "/bin/su"))'

    return profile


class VerificationFailed(Denied):
    """A completed candidate command failed; preserve its bounded evidence for repair."""

    def __init__(self, receipt: Json):
        self.receipt = receipt
        super().__init__("Verification failed: " + str(receipt["name"]))


class Verifier:
    def __init__(self, config: ControllerConfig):
        self.config = config

    async def command(
        self, workspace: Path, recipe: CheckRecipe, *, source_imports: bool = False
    ) -> Json:
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
            raise Denied("Maintenance verification requires the Mac generated-code sandbox")
        scratch = workspace / ".theo"
        if scratch.is_symlink() or not scratch.resolve().is_relative_to(workspace.resolve()):
            raise Denied("Verification scratch space cannot redirect controller operations")
        scratch.mkdir(exist_ok=True)
        home = scratch / "home"
        if home.is_symlink():
            raise Denied("Verification home cannot be a symlink")
        home.mkdir(exist_ok=True)
        # Never take a filesystem grant from a record writable by candidate code.
        records = self.config.root / "temporary-paths"
        records.mkdir(exist_ok=True)
        temp_record = records / (digest({"workspace": str(workspace.resolve())}) + ".json")
        if temp_record.exists():
            temporary = Path(json.loads(temp_record.read_text())["path"])
            if (
                temporary.is_symlink()
                or not temporary.is_dir()
                or temporary.stat().st_uid != os.geteuid()
            ):
                raise Denied("Verification temporary directory no longer belongs to the controller")
        else:
            temporary = Path(tempfile.mkdtemp(prefix="tv-", dir="/tmp"))
            temp_record.write_text(json.dumps({"path": str(temporary)}))
        values = {
            "python": str(self.config.builder_python),
            "uv": str(self.config.uv),
            "workspace": str(workspace),
        }
        argv = list(recipe.argv)
        for key, value in values.items():
            argv = [argument.replace("{" + key + "}", value) for argument in argv]
        if not Path(argv[0]).is_absolute():
            raise Denied("Trusted recipes must name an absolute executable")
        environment = {
            "PATH": str(self.config.builder_python.parent)
            + ":"
            + str(self.config.uv.parent)
            + (":" + str(self.config.node.parent) if self.config.node else "")
            + ":/usr/bin:/bin",
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "THEO_TEST_SOCKET_ROOT": str(temporary),
            "UV_CACHE_DIR": str(scratch / "cache"),
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_PYTHON": str(self.config.builder_python),
            "UV_OFFLINE": "1",
            "THEO_TEST_OFFLINE": "1",
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYRIGHT_PYTHON_GLOBAL_NODE": "on",
        }
        if source_imports:
            environment["PYTHONPATH"] = str(workspace / "src")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(Path(__file__).with_name("worker_launcher.py")),
            str(self.config.root / "builder-process.json"),
            str(recipe.timeout),
            "/usr/bin/sandbox-exec",
            "-p",
            sandbox(workspace, self.config.runtime_reads, temporary),
            *argv,
            cwd=workspace,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        assert process.stdout
        output = bytearray()
        try:
            async with asyncio.timeout(recipe.timeout):
                while chunk := await process.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > 4 * 1024 * 1024:
                        raise Denied("Verification output exceeded limit")
                await process.wait()
            log = self.config.root / "logs" / workspace.name
            log.mkdir(parents=True, exist_ok=True)
            (log / (recipe.name + ".log")).write_bytes(output)
            if process.returncode != 0:
                raise VerificationFailed(
                    {
                        "name": recipe.name,
                        "recipe_hash": digest(recipe.model_dump(mode="json")),
                        "exit_code": process.returncode,
                        "log_sha256": hashlib.sha256(output).hexdigest(),
                    }
                )
            return {
                "name": recipe.name,
                "recipe_hash": digest(recipe.model_dump(mode="json")),
                "exit_code": 0,
                "log_sha256": hashlib.sha256(output).hexdigest(),
                "platform": sys.platform,
            }
        finally:
            await stop_process(process)

    async def prepare_environment(self, workspace: Path) -> Path:
        scratch = workspace / ".theo"
        scratch.mkdir(exist_ok=True)
        wheels = scratch / "wheels"
        if not wheels.exists():
            await asyncio.to_thread(shutil.copytree, self.config.dependency_wheels, wheels)
        for name, argv in (
            (
                "development-export",
                (
                    "{uv}",
                    "export",
                    "--locked",
                    "--all-groups",
                    "--all-extras",
                    "--no-emit-project",
                    "--format",
                    "requirements-txt",
                    "--output-file",
                    ".theo/development.txt",
                ),
            ),
            (
                "development-environment",
                ("{uv}", "venv", "--relocatable", "--python", "{python}", ".theo/environment"),
            ),
            (
                "development-install",
                (
                    "{uv}",
                    "pip",
                    "install",
                    "--python",
                    ".theo/environment/bin/python",
                    "--offline",
                    "--no-index",
                    "--find-links",
                    ".theo/wheels",
                    "--require-hashes",
                    "-r",
                    ".theo/development.txt",
                ),
            ),
        ):
            await self.command(workspace, CheckRecipe(name=name, argv=argv))
        # Preserve the repository's normal uv/Pyright environment location while
        # keeping generated environment files outside the submitted source tree.
        (workspace / ".venv").symlink_to(".theo/environment", target_is_directory=True)
        if self.config.package_checks:
            await self.command(
                workspace,
                CheckRecipe(
                    name="editable-project",
                    argv=(
                        "{uv}",
                        "pip",
                        "install",
                        "--python",
                        str(scratch / "environment/bin/python"),
                        "--offline",
                        "--no-deps",
                        "--no-build-isolation",
                        "-e",
                        ".",
                    ),
                ),
            )
        return scratch / "environment/bin/python"

    async def check(
        self, candidate: CandidateIdentity, source: Path, *, suffix: str = "verify"
    ) -> Json:
        if source_digest(source) != candidate.snapshot_sha256:
            raise Denied("Candidate source identity changed")
        workspace = self.config.workspaces / (candidate.change_id + "-" + suffix)
        if workspace.exists():
            shutil.rmtree(workspace)
        await asyncio.to_thread(write_source, workspace, read_source(source))
        python = await self.prepare_environment(workspace)
        receipts: list[Json] = []
        for recipe in self.config.checks:
            expanded = recipe.model_copy(
                update={
                    "argv": tuple(
                        argument.replace("{python}", str(python)) for argument in recipe.argv
                    )
                }
            )
            receipts.append(
                await self.command(
                    workspace, expanded, source_imports=not self.config.package_checks
                )
            )
        if self.config.package_checks:
            receipts.extend(await self.installed_checks(workspace, python))
        if source_digest(source) != candidate.snapshot_sha256:
            raise Denied("Immutable candidate was modified during verification")
        return {
            "candidate": candidate.model_dump(mode="json"),
            "checks": receipts,
            "recipe_hash": digest([item.model_dump(mode="json") for item in self.config.checks]),
        }

    async def installed_checks(self, source_workspace: Path, python: Path) -> list[Json]:
        """Build both distributions and exercise a wheel outside the source checkout.

        These commands and the smoke script come from the pinned controller;
        deleting the candidate's CI or smoke script cannot disable this gate.
        """
        receipts = [
            await self.command(
                source_workspace,
                CheckRecipe(
                    name="distributions",
                    argv=(
                        "{uv}",
                        "build",
                        "--offline",
                        "--no-build-isolation",
                        "--python",
                        str(python),
                        "--out-dir",
                        ".theo/dist",
                    ),
                ),
            ),
            await self.command(
                source_workspace,
                CheckRecipe(
                    name="production-lock",
                    argv=(
                        "{uv}",
                        "export",
                        "--locked",
                        "--no-dev",
                        "--no-emit-project",
                        "--format",
                        "requirements-txt",
                        "--output-file",
                        ".theo/requirements.txt",
                    ),
                ),
            ),
        ]
        workspace = source_workspace.with_name(source_workspace.name + "-installed")
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir()
        shutil.copytree(self.config.dependency_wheels, workspace / "wheels")
        wheels = list((source_workspace / ".theo/dist").glob("*.whl"))
        if len(wheels) != 1:
            raise Denied("Installed verification requires exactly one candidate wheel")
        output_file(wheels[0], source_workspace)
        output_file(
            source_workspace / ".theo/requirements.txt", source_workspace, limit=1024 * 1024
        )
        shutil.copyfile(wheels[0], workspace / wheels[0].name)
        shutil.copyfile(source_workspace / ".theo/requirements.txt", workspace / "requirements.txt")
        # This file is read from the installed controller, never from candidate source.
        script = Path(__file__).with_name("installed_check.txt")
        shutil.copyfile(script, workspace / "installed_check.py")
        for name, argv in (
            ("installed-environment", ("{uv}", "venv", "--python", "{python}", "environment")),
            (
                "installed-dependencies",
                (
                    "{uv}",
                    "pip",
                    "install",
                    "--python",
                    "environment/bin/python",
                    "--offline",
                    "--no-index",
                    "--find-links",
                    "wheels",
                    "--require-hashes",
                    "-r",
                    "requirements.txt",
                ),
            ),
            (
                "installed-wheel",
                (
                    "{uv}",
                    "pip",
                    "install",
                    "--python",
                    "environment/bin/python",
                    "--offline",
                    "--no-deps",
                    wheels[0].name,
                ),
            ),
            (
                "installed-smoke",
                (str(workspace / "environment/bin/python"), "-I", "installed_check.py"),
            ),
        ):
            receipts.append(await self.command(workspace, CheckRecipe(name=name, argv=argv)))
        receipts.append(
            {
                "name": "installed-artifacts",
                "exit_code": 0,
                "wheel_sha256": hashlib.sha256(wheels[0].read_bytes()).hexdigest(),
                "trusted_smoke_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            }
        )
        return receipts

    async def package(self, candidate: CandidateIdentity, source: Path, verification: Json) -> Json:
        from theo.maintenance.bundles import Bundle, inventory, verify

        workspace = self.config.workspaces / (candidate.change_id + "-package")
        if workspace.exists():
            shutil.rmtree(workspace)
        await asyncio.to_thread(write_source, workspace, read_source(source))
        scratch = workspace / ".theo"
        scratch.mkdir()
        if not self.config.dependency_wheels.is_dir():
            raise Denied("Stage hash-verified dependency wheels before packaging")
        await asyncio.to_thread(shutil.copytree, self.config.dependency_wheels, scratch / "wheels")
        python = await self.prepare_environment(workspace)
        commands: list[tuple[str, tuple[str, ...]]] = [
            (
                "export",
                (
                    "{uv}",
                    "export",
                    "--locked",
                    "--no-dev",
                    "--no-emit-project",
                    "--format",
                    "requirements-txt",
                    "--output-file",
                    ".theo/requirements.txt",
                ),
            ),
            (
                "wheel",
                (
                    "{uv}",
                    "build",
                    "--offline",
                    "--no-build-isolation",
                    "--python",
                    "{python}",
                    "--wheel",
                    "--out-dir",
                    ".theo/dist",
                ),
            ),
            (
                "environment",
                ("{uv}", "venv", "--relocatable", "--python", "{python}", ".theo/bundle/core"),
            ),
            (
                "dependencies",
                (
                    "{uv}",
                    "pip",
                    "install",
                    "--python",
                    ".theo/bundle/core/bin/python",
                    "--offline",
                    "--no-index",
                    "--find-links",
                    ".theo/wheels",
                    "--require-hashes",
                    "-r",
                    ".theo/requirements.txt",
                ),
            ),
        ]
        for name, argv in commands:
            await self.command(
                workspace,
                CheckRecipe(
                    name=name,
                    argv=tuple(argument.replace("{python}", str(python)) for argument in argv),
                ),
            )
        wheels = list((scratch / "dist").glob("*.whl"))
        if len(wheels) != 1:
            raise Denied("Build must produce exactly one distribution wheel")
        output_file(wheels[0], workspace)
        await self.command(
            workspace,
            CheckRecipe(
                name="install",
                argv=(
                    "{uv}",
                    "pip",
                    "install",
                    "--python",
                    ".theo/bundle/core/bin/python",
                    "--offline",
                    "--no-deps",
                    str(wheels[0]),
                ),
            ),
        )
        bundle_root = scratch / "bundle"
        if bundle_root.is_symlink() or not bundle_root.resolve().is_relative_to(
            workspace.resolve()
        ):
            raise Denied("Build bundle escapes its workspace")
        for path in bundle_root.rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(bundle_root.resolve()):
                relative = path.relative_to(bundle_root).parts
                if (
                    len(relative) != 3
                    or relative[0] != "core"
                    or relative[1] != "bin"
                    or relative[2] not in ("python", "python3", "python3.14")
                    or path.resolve() != self.config.builder_python.resolve()
                ):
                    raise Denied("Build output contains an unapproved external runtime link")
            elif path.is_file() and not path.is_symlink():
                output_file(path, workspace)
        # Both environments originate from the same wheel and lock in this immutable revision.
        await asyncio.to_thread(
            shutil.copytree, bundle_root / "core", bundle_root / "worker", symlinks=True
        )
        for path in bundle_root.rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(bundle_root):
                target = path.resolve(strict=True)
                if not target.is_file():
                    raise Denied("Unsupported external runtime directory link")
                path.unlink()
                shutil.copyfile(target, path)
                path.chmod(target.stat().st_mode & 0o777)
        for name in ("core", "worker"):
            python = str(bundle_root / name / "bin/python")
            await self.command(
                workspace,
                CheckRecipe(
                    name=name + "-canary",
                    argv=(
                        python,
                        "-m",
                        "theo",
                        "--data-root",
                        str(scratch / (name + "-canary")),
                        "init",
                    ),
                ),
            )
            await self.command(
                workspace,
                CheckRecipe(
                    name=name + "-doctor",
                    argv=(
                        python,
                        "-m",
                        "theo",
                        "--data-root",
                        str(scratch / (name + "-canary")),
                        "doctor",
                        "--json",
                    ),
                ),
            )
        for name, runtime in self.config.native_files.items():
            destination = bundle_root / "native" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if runtime.is_dir():
                shutil.copytree(runtime, destination, symlinks=False)
            else:
                shutil.copy2(runtime, destination)
        schema = len(list((source / "src/theo/migrations").glob("*.sql")))
        (bundle_root / "bin").mkdir()
        (bundle_root / "bin/python").symlink_to("../core/bin/python")
        bundle = Bundle(
            bundle_id=candidate.change_id + "-" + candidate.commit[:12],
            source_sha=candidate.commit,
            tree=candidate.tree,
            schema_min=schema,
            schema_max=schema,
            files=inventory(bundle_root),
            verification_hash=digest(verification),
        )
        (bundle_root / "bundle.json").write_text(bundle.model_dump_json(indent=2))
        target = self.config.root / "bundles" / bundle.bundle_id
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = verify(target)
            if existing.source_sha != candidate.commit or existing.verification_hash != digest(
                verification
            ):
                raise Denied("Bundle ID already binds different source or checks")
            return {"bundle_id": existing.bundle_id, "fingerprint": existing.fingerprint}
        shutil.copytree(bundle_root, target, symlinks=True)
        accepted = verify(target, bundle.fingerprint)
        for path in target.rglob("*"):
            if path.is_file() and not path.is_symlink():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        return {"bundle_id": accepted.bundle_id, "fingerprint": accepted.fingerprint}
