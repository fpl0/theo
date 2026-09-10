"""Build matching core/worker prefixes in a VM and accept their sealed bytes.

Candidate installers and relocation canaries execute only in the disposable
guest. The controller extracts regular files, checks the complete manifest and
adds independently configured native inputs without executing candidate code.
"""

import asyncio
import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path

from theo.domain import Denied, Json, digest
from theo.maintenance.bundle_archive import extract
from theo.maintenance.bundles import Bundle, NativeSelection, inventory, native_settings, verify
from theo.maintenance.configuration import CheckRecipe
from theo.maintenance.contracts import CandidateIdentity
from theo.maintenance.source import source_digest
from theo.maintenance.vm_driver import GUEST_PYTHON, GUEST_ROOT, VmDriver
from theo.maintenance.vm_verification import SCRATCH, UV, WHEELS, WORK, VmVerifier

BUILD = WORK + "/bundle-build"
SEALED = GUEST_ROOT + "/sealed-bundle"
HELPER = GUEST_ROOT + "/tools/vm_bundle.py"


class VmPackager(VmVerifier):
    async def build_prefixes(
        self,
        vm: VmDriver,
        verification: Json | None,
        source_sha256: str,
        *,
        development: bool = False,
    ) -> None:
        if verification is None and not development:
            raise Denied("A release prefix requires independent candidate verification")
        await self.prepare(vm)
        await self.installed_checks(vm)
        assert self.wheel
        prior = [
            item
            for item in (verification or {}).get("checks", [])
            if item.get("name") == "installed-artifacts"
        ]
        if verification is not None and (
            len(prior) != 1 or prior[0].get("wheel_sha256") != self.wheel["sha256"]
        ):
            raise Denied("The rebuilt wheel differs from the independently verified candidate")
        await self.command(
            vm, CheckRecipe(name="bundle-prefix", argv=(GUEST_PYTHON, "-I", HELPER, "prepare"))
        )
        await self.command(
            vm,
            CheckRecipe(
                name="bundle-lock",
                argv=(
                    UV,
                    "export",
                    "--locked",
                    *(("--all-groups", "--all-extras") if development else ("--no-dev",)),
                    "--no-emit-project",
                    *(
                        value
                        for extra in self.config.runtime_extras
                        for value in ("--extra", extra)
                    ),
                    "--format",
                    "requirements-txt",
                    "--output-file",
                    SCRATCH + "/bundle-requirements.txt",
                ),
            ),
        )
        # --break-system-packages applies only to the new, unprivileged guest
        # prefix. The pinned archive includes uv's externally-managed marker;
        # neither the guest tooling prefix nor any host interpreter is modified.
        for name, args in (
            (
                "bundle-dependencies",
                (
                    "--offline",
                    "--no-index",
                    "--find-links",
                    WHEELS,
                    "--require-hashes",
                    "-r",
                    SCRATCH + "/bundle-requirements.txt",
                ),
            ),
            (
                "bundle-wheel",
                ("--offline", "--no-deps", SCRATCH + "/dist/" + self.wheel["name"]),
            ),
        ):
            await self.command(
                vm,
                CheckRecipe(
                    name=name,
                    argv=(
                        UV,
                        "pip",
                        "install",
                        "--python",
                        BUILD + "/core/bin/python",
                        "--system",
                        "--break-system-packages",
                        *args,
                    ),
                ),
            )
        await self.command(
            vm, CheckRecipe(name="bundle-relocate", argv=(GUEST_PYTHON, "-I", HELPER, "finish"))
        )
        await self.command(
            vm, CheckRecipe(name="bundle-clean", argv=(GUEST_PYTHON, "-I", HELPER, "clean"))
        )
        # The canaries must exercise the sealed bytes. A passed canary on a
        # still-writable prefix cannot authorize whatever bytes appear later.
        sealed = await vm.seal_bundle(source_sha256)
        self.receipts.append({"name": "sealed-runtime", **sealed})
        permissions = (
            "from pathlib import Path; import os,sys\n"
            "for argument in sys.argv[1:]:\n"
            " prefix=Path(argument)\n"
            " for path,flags in ((prefix/'lib/python3.14/os.py',os.O_WRONLY),"
            "(prefix/'write-canary',os.O_WRONLY|os.O_CREAT|os.O_EXCL)):\n"
            "  try: descriptor=os.open(path,flags,0o600)\n"
            "  except PermissionError: continue\n"
            "  else: os.close(descriptor); raise RuntimeError('Sealed runtime is writable')\n"
            "print('Both sealed prefixes denied existing-file and directory writes')"
        )
        await self.command(
            vm,
            CheckRecipe(
                name="sealed-prefix-permissions",
                argv=(GUEST_PYTHON, "-I", "-c", permissions, SEALED + "/core", SEALED + "/worker"),
            ),
            cwd=WORK,
        )
        for environment in ("core", "worker"):
            prefix = SEALED + "/" + environment
            for name, argv in (
                (
                    "installed-smoke",
                    (prefix + "/bin/python", "-I", "-B", GUEST_ROOT + "/inputs/installed_check.py"),
                ),
                ("console", (prefix + "/bin/theo", "--help")),
                (
                    "initialize",
                    (
                        prefix + "/bin/python",
                        "-I",
                        "-B",
                        "-m",
                        "theo",
                        "--data-root",
                        WORK + "/" + environment + "-canary",
                        "init",
                    ),
                ),
                (
                    "doctor",
                    (
                        prefix + "/bin/python",
                        "-I",
                        "-B",
                        "-m",
                        "theo",
                        "--data-root",
                        WORK + "/" + environment + "-canary",
                        "doctor",
                        "--json",
                    ),
                ),
            ):
                await self.command(
                    vm, CheckRecipe(name=environment + "-" + name, argv=argv), cwd=WORK
                )

    def add_native(self, target: Path) -> None:
        for name, path in self.config.native_files.items():
            if name == "runtime.json":
                raise Denied("Native selection metadata belongs to the controller")
            destination = target / "native" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                raise Denied("Pinned native input cannot be a symlink")
            if path.is_dir():
                before = inventory(path)
                shutil.copytree(path, destination, symlinks=True)
                if inventory(path) != before or inventory(destination) != before:
                    raise Denied("Native directory changed while it was copied")
            else:
                info = path.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > 1_000_000_000
                ):
                    raise Denied("Pinned native input must be a bounded regular file")
                with path.open("rb") as stream:
                    before = hashlib.file_digest(stream, "sha256").hexdigest()
                shutil.copy2(path, destination)
                with path.open("rb") as source, destination.open("rb") as copy:
                    if (
                        before != hashlib.file_digest(source, "sha256").hexdigest()
                        or before != hashlib.file_digest(copy, "sha256").hexdigest()
                    ):
                        raise Denied("Native executable changed while it was copied")
        selection = NativeSelection(executables=self.config.native_executables)
        (target / "native").mkdir(exist_ok=True)
        (target / "native/runtime.json").write_text(selection.model_dump_json(indent=2))

    async def package(self, candidate: CandidateIdentity, source: Path, verification: Json) -> Json:
        if (
            source_digest(source) != candidate.snapshot_sha256
            or verification.get("candidate") != candidate.model_dump(mode="json")
            or verification.get("vm_stopped") is not True
        ):
            raise Denied("Packaging requires completed verification of this exact source")
        bundle_id = candidate.change_id + "-" + candidate.commit[:12]
        target = self.config.bundles / bundle_id
        if not target.parent.exists():
            target.parent.mkdir(mode=0o750, parents=True)
            os.chown(target.parent, -1, self.config.core_gid)
            target.parent.chmod(0o750)
        if (
            target.parent.is_symlink()
            or target.parent.stat().st_uid != os.geteuid()
            or target.parent.stat().st_mode & 0o022
        ):
            raise Denied(
                "Accepted bundle storage must be controller-owned and non-writable by peers"
            )
        if target.exists():
            existing = verify(target)
            if existing.source_sha != candidate.commit or existing.verification_hash != digest(
                verification
            ):
                raise Denied("Bundle identity already binds different source or verification")
            return {"bundle_id": bundle_id, "fingerprint": existing.fingerprint}
        async with VmDriver(self.vm_settings) as vm:
            await vm.prepare(source, self.config.dependency_wheels)
            await self.build_prefixes(vm, verification, candidate.snapshot_sha256)
            archive, export = await vm.export_bundle(candidate.snapshot_sha256)
        if source_digest(source) != candidate.snapshot_sha256:
            raise Denied("Immutable candidate changed during packaging")
        # Stage on the destination filesystem so acceptance remains an atomic
        # rename even when the large disposable VM store is on another volume.
        staging = target.parent / (".staging-" + uuid.uuid4().hex)
        await asyncio.to_thread(extract, archive, staging)
        if set(path.name for path in staging.iterdir()) != {"core", "worker", "uv.lock"} or any(
            (staging / name).is_symlink() or not (staging / name).is_dir()
            for name in ("core", "worker")
        ):
            raise Denied("Guest output must contain only the matching prefixes and dependency lock")
        if await asyncio.to_thread(inventory, staging / "core") != await asyncio.to_thread(
            inventory, staging / "worker"
        ):
            raise Denied("Core and worker prefixes differ after their relocation canaries")
        if hashlib.sha256((staging / "uv.lock").read_bytes()).hexdigest() != candidate.lock_sha256:
            raise Denied("Built bundle does not contain its accepted dependency lock")
        await asyncio.to_thread(self.add_native, staging)
        (staging / "verification.json").write_text(json.dumps(verification, indent=2))
        (staging / "packaging.json").write_text(
            json.dumps({"checks": self.receipts, "export": export}, indent=2)
        )
        (staging / "bin").mkdir()
        (staging / "bin/python").symlink_to("../core/bin/python")
        # The core and runner need read/execute access without traversal into
        # private controller state. Peers cannot write accepted bundle files.
        for path in staging.rglob("*"):
            os.chown(path, -1, self.config.core_gid, follow_symlinks=False)
            if path.is_file() and not path.is_symlink():
                path.chmod(0o550 if path.stat().st_mode & 0o111 else 0o440)
        schema = len(list((source / "src/theo/migrations").glob("*.sql")))
        bundle = Bundle(
            bundle_id=bundle_id,
            source_sha=candidate.commit,
            tree=candidate.tree,
            schema_min=schema,
            schema_max=schema,
            files=await asyncio.to_thread(inventory, staging),
            verification_hash=digest(verification),
        )
        # This also refuses a missing Codex code-mode helper. Native assets come
        # from protected installation wiring, never from the candidate's tree.
        native_settings(staging, bundle)
        descriptor_path = staging / "bundle.json"
        descriptor_path.write_text(bundle.model_dump_json(indent=2))
        os.chown(descriptor_path, -1, self.config.core_gid)
        descriptor_path.chmod(0o440)
        await asyncio.to_thread(verify, staging, bundle.fingerprint)
        for path in staging.rglob("*"):
            if path.is_file() and not path.is_symlink():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        for path in (staging, *staging.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                os.chown(path, -1, self.config.core_gid)
                # macOS 15 also requires write permission on the directory
                # being renamed. Its owner is the controller; peer access
                # remains read/execute only throughout atomic acceptance.
                path.chmod(0o750 if path == staging else 0o550)
        staging.rename(target)
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return {"bundle_id": bundle_id, "fingerprint": bundle.fingerprint}
