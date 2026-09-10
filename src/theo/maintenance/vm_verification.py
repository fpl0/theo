"""Run controller-selected acceptance recipes inside one disposable Mac guest.

The host supplies immutable source, pinned tooling and locked wheels. Commands
execute through the guest's unprivileged receipt runner, and the complete VM is
stopped before a verification receipt can reach publication or deployment.
"""

import hashlib
import json
from pathlib import Path
from typing import cast

from theo.domain import Denied, Json, digest
from theo.maintenance.configuration import CheckRecipe, ControllerConfig
from theo.maintenance.contracts import CandidateIdentity, VerificationFailed
from theo.maintenance.minimum import acceptance_files
from theo.maintenance.source import files_digest, read_source, source_digest
from theo.maintenance.vm_driver import GUEST_PYTHON, GUEST_ROOT, GUEST_SOURCE, VmDriver

UV = GUEST_ROOT + "/tools/uv"
WHEELS = GUEST_ROOT + "/tools/wheels"
WORK = GUEST_ROOT + "/work"
SCRATCH = WORK + "/verification"
PYTHON = SCRATCH + "/environment/bin/python"
INSTALLED = WORK + "/installed"
MINIMUM = GUEST_ROOT + "/minimum-source"
MINIMUM_ENV = GUEST_ROOT + "/minimum-environment"
MINIMUM_PYTHON = MINIMUM_ENV + "/bin/python"


class VmVerifier:
    def __init__(self, config: ControllerConfig):
        if config.vm is None:
            raise Denied("The VM verifier requires a pinned guest configuration")
        self.config = config
        self.vm_settings = config.vm
        self.receipts: list[Json] = []
        self.wheel: Json | None = None

    async def command(self, vm: VmDriver, recipe: CheckRecipe, *, cwd: str = GUEST_SOURCE) -> Json:
        values = {"python": PYTHON, "uv": UV, "workspace": GUEST_SOURCE}
        argv = list(recipe.argv)
        for key, value in values.items():
            argv = [argument.replace("{" + key + "}", value) for argument in argv]
        identity = f"check-{len(self.receipts)}-{digest(recipe.model_dump(mode='json'))[:12]}"
        result = await vm.recipe(identity, argv, cwd, recipe.timeout)
        receipt: Json = {
            "name": recipe.name,
            "recipe_hash": digest(recipe.model_dump(mode="json")),
            "argv": argv,
            "cwd": cwd,
            "exit_code": result["exit_code"],
            "limit": result["limit"],
            "log_sha256": result["output_sha256"],
            "request_sha256": result["request_sha256"],
            "guest_uid": result["guest_uid"],
            "descendants_stopped": result["descendants_stopped"],
        }
        self.receipts.append(receipt)
        if result["exit_code"] != 0 or result["limit"] is not None:
            raise VerificationFailed(receipt)
        return receipt

    async def prepare(self, vm: VmDriver) -> None:
        for name, argv in (
            (
                "development-scratch",
                (
                    GUEST_PYTHON,
                    "-I",
                    "-c",
                    f"from pathlib import Path; Path({SCRATCH!r}).mkdir(); "
                    "Path('.venv').symlink_to('../verification/environment',target_is_directory=True)",
                ),
            ),
            (
                "development-export",
                (
                    UV,
                    "export",
                    "--locked",
                    "--all-groups",
                    "--all-extras",
                    "--no-emit-project",
                    "--format",
                    "requirements-txt",
                    "--output-file",
                    SCRATCH + "/development.txt",
                ),
            ),
            (
                "development-environment",
                (UV, "venv", "--python", GUEST_PYTHON, SCRATCH + "/environment"),
            ),
            (
                "development-install",
                (
                    UV,
                    "pip",
                    "install",
                    "--python",
                    PYTHON,
                    "--offline",
                    "--no-index",
                    "--find-links",
                    WHEELS,
                    "--require-hashes",
                    "-r",
                    SCRATCH + "/development.txt",
                ),
            ),
            (
                "editable-project",
                (
                    UV,
                    "pip",
                    "install",
                    "--python",
                    PYTHON,
                    "--offline",
                    "--no-deps",
                    "--no-build-isolation",
                    "-e",
                    ".",
                ),
            ),
        ):
            await self.command(vm, CheckRecipe(name=name, argv=argv))

    async def minimum_checks(self, vm: VmDriver, expected: str) -> None:
        """Run the previous acceptance recipe before admitting candidate packaging hooks."""
        for name, argv in (
            (
                "minimum-export",
                (
                    UV,
                    "export",
                    "--locked",
                    "--all-groups",
                    "--all-extras",
                    "--no-emit-project",
                    "--format",
                    "requirements-txt",
                    "--output-file",
                    WORK + "/minimum-requirements.txt",
                ),
            ),
            (
                "minimum-environment",
                (UV, "venv", "--allow-existing", "--python", GUEST_PYTHON, MINIMUM_ENV),
            ),
            (
                "minimum-install",
                (
                    UV,
                    "pip",
                    "install",
                    "--python",
                    MINIMUM_PYTHON,
                    "--offline",
                    "--no-index",
                    "--find-links",
                    WHEELS,
                    "--require-hashes",
                    "--link-mode",
                    "copy",
                    "-r",
                    WORK + "/minimum-requirements.txt",
                ),
            ),
        ):
            await self.command(vm, CheckRecipe(name=name, argv=argv), cwd=MINIMUM)
        helper = [GUEST_PYTHON, "-I", "-B", GUEST_ROOT + "/tools/vm_minimum.py"]
        await vm.guest([*helper, "seal"], timeout=120)
        permission_canary = (
            "from pathlib import Path; import sys,theo\n"
            f"assert Path(theo.__file__).resolve()==Path({MINIMUM + '/src/theo/__init__.py'!r})\n"
            "for name in sys.argv[1:]:\n"
            " try: Path(name).open('ab').close()\n"
            " except PermissionError: pass\n"
            " else: raise AssertionError('Candidate can write minimum acceptance files')\n"
            "print('Minimum tests, configuration and tools are read-only to the build UID')"
        )
        await self.command(
            vm,
            CheckRecipe(
                name="minimum-permission-canary",
                argv=(
                    MINIMUM_PYTHON,
                    "-I",
                    "-B",
                    "-c",
                    permission_canary,
                    MINIMUM + "/tests/conftest.py",
                    MINIMUM + "/pyproject.toml",
                    MINIMUM_ENV + "/lib/python3.14/site-packages/pytest/__init__.py",
                ),
            ),
            cwd=MINIMUM,
        )
        for name, argv in (
            (
                "minimum-ruff",
                (
                    MINIMUM_PYTHON,
                    "-I",
                    "-B",
                    "-m",
                    "ruff",
                    "check",
                    "--no-cache",
                    "src",
                    "tests",
                    "scripts",
                ),
            ),
            (
                "minimum-format",
                (
                    MINIMUM_PYTHON,
                    "-I",
                    "-B",
                    "-m",
                    "ruff",
                    "format",
                    "--check",
                    "--no-cache",
                    "src",
                    "tests",
                    "scripts",
                ),
            ),
            ("minimum-pyright", (MINIMUM_PYTHON, "-I", "-B", "-m", "pyright")),
            (
                "minimum-pytest",
                (
                    MINIMUM_PYTHON,
                    "-I",
                    "-B",
                    "-m",
                    "pytest",
                    "-q",
                    "-o",
                    "cache_dir=" + WORK + "/cache/minimum-pytest",
                ),
            ),
        ):
            await self.command(vm, CheckRecipe(name=name, argv=argv), cwd=MINIMUM)
        receipt = json.loads(await vm.guest([*helper, "check"], timeout=60))
        if receipt != {"minimum_sha256": expected, "sealed": True}:
            raise Denied("Minimum verification source or tooling lost its protection")
        self.receipts.append({"name": "minimum-protected-source", "exit_code": 0, **receipt})

    async def installed_checks(self, vm: VmDriver) -> None:
        await self.command(
            vm,
            CheckRecipe(
                name="source-environment-unlink",
                argv=(GUEST_PYTHON, "-I", "-c", "from pathlib import Path; Path('.venv').unlink()"),
            ),
        )
        await self.command(
            vm,
            CheckRecipe(
                name="distributions",
                argv=(
                    UV,
                    "build",
                    "--offline",
                    "--no-build-isolation",
                    "--python",
                    PYTHON,
                    "--out-dir",
                    SCRATCH + "/dist",
                ),
            ),
        )
        await self.command(
            vm,
            CheckRecipe(
                name="production-lock",
                argv=(
                    UV,
                    "export",
                    "--locked",
                    "--no-dev",
                    "--no-emit-project",
                    "--format",
                    "requirements-txt",
                    "--output-file",
                    SCRATCH + "/requirements.txt",
                ),
            ),
        )
        # Inspect bytes with the protected interpreter after the recipe runner
        # has reaped the entire build UID. No candidate code runs as guest root.
        inspect = (
            "import pathlib,stat,hashlib,json; "
            f"root=pathlib.Path({SCRATCH!r}); "
            "wheels=list((root/'dist').glob('*.whl')); assert len(wheels)==1; "
            "p=wheels[0]; s=p.lstat(); "
            "assert p.resolve().is_relative_to(root) and stat.S_ISREG(s.st_mode) "
            "and s.st_nlink==1 and s.st_size<=268435456; "
            "print(json.dumps({'name':p.name,'sha256':hashlib.file_digest(p.open('rb'),'sha256').hexdigest()}))"
        )
        wheel = cast(Json, json.loads(await vm.guest([GUEST_PYTHON, "-I", "-c", inspect])))
        if (
            not isinstance(wheel.get("name"), str)
            or Path(wheel["name"]).name != wheel["name"]
            or not wheel["name"].endswith(".whl")
        ):
            raise Denied("The guest did not report one bounded wheel")
        self.wheel = wheel
        copy = (
            "from pathlib import Path; import shutil; "
            f"p=Path({INSTALLED!r}); p.mkdir(); "
            f"shutil.copyfile({SCRATCH + '/dist/' + wheel['name']!r},p/{wheel['name']!r}); "
            f"shutil.copyfile({SCRATCH + '/requirements.txt'!r},p/'requirements.txt')"
        )
        await self.command(
            vm, CheckRecipe(name="installed-copy", argv=(GUEST_PYTHON, "-I", "-c", copy))
        )
        for name, argv in (
            ("installed-environment", (UV, "venv", "--python", GUEST_PYTHON, "environment")),
            (
                "installed-dependencies",
                (
                    UV,
                    "pip",
                    "install",
                    "--python",
                    "environment/bin/python",
                    "--offline",
                    "--no-index",
                    "--find-links",
                    WHEELS,
                    "--require-hashes",
                    "-r",
                    "requirements.txt",
                ),
            ),
            (
                "installed-wheel",
                (
                    UV,
                    "pip",
                    "install",
                    "--python",
                    "environment/bin/python",
                    "--offline",
                    "--no-deps",
                    wheel["name"],
                ),
            ),
            (
                "installed-smoke",
                (
                    INSTALLED + "/environment/bin/python",
                    "-I",
                    GUEST_ROOT + "/inputs/installed_check.py",
                ),
            ),
        ):
            await self.command(vm, CheckRecipe(name=name, argv=argv), cwd=INSTALLED)
        self.receipts.append(
            {
                "name": "installed-artifacts",
                "exit_code": 0,
                "wheel_sha256": wheel["sha256"],
                "trusted_smoke_sha256": hashlib.sha256(
                    Path(__file__).with_name("installed_check.txt").read_bytes()
                ).hexdigest(),
            }
        )

    async def check(self, candidate: CandidateIdentity, source: Path) -> Json:
        if source_digest(source) != candidate.snapshot_sha256:
            raise Denied("Candidate source identity changed")
        minimum = None
        if self.config.minimum_source and self.config.minimum_source_sha256:
            minimum = acceptance_files(
                self.config.minimum_source, self.config.minimum_source_sha256, read_source(source)
            )
        async with VmDriver(self.vm_settings) as vm:
            if minimum is not None:
                await vm.prepare(source, self.config.dependency_wheels, minimum=minimum)
                await self.minimum_checks(vm, files_digest(minimum))
            else:
                await vm.prepare(source, self.config.dependency_wheels)
            await self.prepare(vm)
            for recipe in self.config.checks:
                await self.command(vm, recipe)
            await self.installed_checks(vm)
            await vm.source_check(candidate.snapshot_sha256)
            directory = vm.directory
        if source_digest(source) != candidate.snapshot_sha256:
            raise Denied("Immutable candidate was modified during verification")
        result: Json = {
            "candidate": candidate.model_dump(mode="json"),
            "checks": self.receipts,
            "recipe_hash": digest([item.model_dump(mode="json") for item in self.config.checks]),
            "platform": "darwin-arm64-vm",
            "image_digest": self.vm_settings.image_digest,
            "interpreter_archive_sha256": self.vm_settings.python_sha256,
            "agent_sha256": self.vm_settings.agent_sha256,
            "vm_stopped": True,
            "minimum_baseline_sha256": self.config.minimum_source_sha256,
            "minimum_acceptance_sha256": files_digest(minimum) if minimum is not None else None,
        }
        (directory / "verification.json").write_text(json.dumps(result, indent=2))
        return result
