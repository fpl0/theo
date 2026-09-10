"""Bounded source ingestion and controller-owned Git objects.

Worker Git configuration, hooks, links, caches and credentials are never imported.
Accepted bytes are independently copied and hashed before any checks or publication.
"""

import asyncio
import hashlib
import os
import re
import shutil
import stat
from pathlib import Path

from theo.backends.process import stop_process
from theo.domain import Conflict, Denied, Json, digest
from theo.maintenance.contracts import CandidateIdentity

IGNORED = frozenset(
    {
        ".git",
        ".venv",
        ".theo",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".hypothesis",
        "dist",
        "build",
        ".DS_Store",
    }
)
SECRET = re.compile(
    rb"(?m)^-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bAKIA[A-Z0-9]{16}\b"
)
MAX_BYTES = 100 * 1024 * 1024


def read_source(root: Path) -> dict[str, tuple[bytes, int]]:
    if root.is_symlink() or not root.is_dir():
        raise Denied("Source must be a real directory")
    files: dict[str, tuple[bytes, int]] = {}
    size = 0
    for directory, dirs, names, descriptor in os.fwalk(root, follow_symlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in IGNORED)
        for name in dirs:
            if stat.S_ISLNK(os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode):
                raise Denied("Source directory links are prohibited")
        for name in sorted(names):
            if name in IGNORED or name.endswith(".pyc"):
                continue
            relative = str((Path(directory) / name).relative_to(root))
            if (
                name in {".env", ".gitmodules"}
                or name.endswith((".sqlite3", ".sqlite", ".pem", ".key"))
                or any(ord(c) < 32 for c in relative)
            ):
                raise Denied("Private or unsafe source path")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            try:
                before = os.fstat(fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_size > 4 * 1024 * 1024
                ):
                    raise Denied("Source must contain bounded regular files without hard links")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    body = stream.read(4 * 1024 * 1024 + 1)
                after = os.fstat(fd)
                if before.st_mtime_ns != after.st_mtime_ns or len(body) != before.st_size:
                    raise Conflict("Source changed during submission")
            finally:
                os.close(fd)
            size += len(body)
            if len(files) >= 10000 or size > MAX_BYTES:
                raise Denied("Source snapshot exceeds installation bounds")
            if SECRET.search(body):
                raise Denied("Source contains credential material; remove it before publication")
            files[relative] = (body, 0o755 if before.st_mode & 0o111 else 0o644)
    return files


def files_digest(files: dict[str, tuple[bytes, int]]) -> str:
    return digest(
        {
            name: [hashlib.sha256(body).hexdigest(), mode]
            for name, (body, mode) in sorted(files.items())
        }
    )


def source_digest(root: Path) -> str:
    return files_digest(read_source(root))


def write_source(root: Path, files: dict[str, tuple[bytes, int]]) -> None:
    root.mkdir(parents=True, exist_ok=False)
    for name, (body, mode) in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        target.chmod(mode)


def git_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": "/var/empty",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https:file",
        "GIT_AUTHOR_NAME": "Theo",
        "GIT_AUTHOR_EMAIL": "theo@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "Theo",
        "GIT_COMMITTER_EMAIL": "theo@users.noreply.github.com",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }


async def git(
    path: Path, *args: str, env: dict[str, str] | None = None, timeout: int = 120, trim: bool = True
) -> str:
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
        "-C",
        str(path),
        *args,
        env=env or git_environment(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    assert process.stdout
    body = bytearray()
    try:
        async with asyncio.timeout(timeout):
            while chunk := await process.stdout.read(65536):
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise Denied("Git output exceeds bound")
            await process.wait()
        if process.returncode:
            raise Conflict("Git operation failed")
        result = body.decode()
        return result.strip() if trim else result
    finally:
        await stop_process(process)


class Source:
    def __init__(self, root: Path, workspaces: Path):
        self.root, self.workspaces = root, workspaces
        self.cache = root / "repository"

    async def fetch(self, url: str, branch: str, env: dict[str, str] | None = None) -> str:
        if not self.cache.exists():
            self.cache.mkdir(parents=True)
            await git(self.cache, "init", "--bare")
        await git(self.cache, "fetch", "--no-tags", url, "refs/heads/" + branch, env=env)
        commit = await git(self.cache, "rev-parse", "FETCH_HEAD")
        await git(self.cache, "update-ref", "refs/heads/source", commit)
        return commit

    async def checkout(self, commit: str, destination: Path) -> None:
        if destination.exists():
            raise Conflict("Workspace already exists without a preparation receipt")
        destination.parent.mkdir(parents=True, exist_ok=True)
        await git(
            destination.parent,
            "clone",
            "--no-local",
            "--no-checkout",
            str(self.cache),
            str(destination),
        )
        await git(destination, "checkout", "--detach", commit)
        await asyncio.to_thread(read_source, destination)

    async def accept(
        self, change_id: str, revision: int, job_id: str, base: str, expected_digest: str
    ) -> CandidateIdentity:
        target = self.root / "candidates" / change_id / str(revision)
        if target.exists():
            files = await asyncio.to_thread(read_source, target)
        else:
            files = await asyncio.to_thread(read_source, self.workspaces / job_id)
            if files_digest(files) != expected_digest:
                raise Conflict("Workspace changed after submission")
            await self.checkout(base, target)
            for child in target.iterdir():
                if child.name != ".git":
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
            for name, (body, mode) in files.items():
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                path.chmod(mode)
        if files_digest(files) != expected_digest:
            raise Conflict("Immutable candidate differs from submitted bytes")
        # Recover the gap between copying accepted bytes and committing them.
        # Include newly ignored paths: .gitignore cannot omit submitted code.
        await git(target, "add", "--all", "--force")
        tree = await git(target, "write-tree")
        commit = await git(
            target, "commit-tree", tree, "-p", base, "-m", "Theo maintenance " + change_id
        )
        await git(target, "update-ref", "HEAD", commit)
        lock = files.get("uv.lock")
        if not lock:
            raise Denied("Candidate lacks the dependency lock")
        return CandidateIdentity(
            change_id=change_id,
            revision=revision,
            base_commit=base,
            commit=await git(target, "rev-parse", "HEAD"),
            tree=await git(target, "rev-parse", "HEAD^{tree}"),
            snapshot_sha256=expected_digest,
            lock_sha256=hashlib.sha256(lock[0]).hexdigest(),
        )

    async def prepare_revision(
        self, candidate: CandidateIdentity, base: str, destination: Path
    ) -> Json:
        """Apply the accepted patch onto the selected base in a fresh coding workspace.

        Git metadata comes from our own cache. Conflicts remain visible to the
        coding job; no review or verification result survives this integration.
        """
        await self.evidence(candidate)
        prior = self.root / "candidates" / candidate.change_id / str(candidate.revision)
        await self.checkout(base, destination)
        await git(destination, "fetch", "--no-tags", str(prior), candidate.commit)
        patch = await git(
            prior, "diff", "--binary", candidate.base_commit, candidate.commit, trim=False
        )
        metadata = destination / ".theo"
        metadata.mkdir(exist_ok=False)
        patch_path = metadata / "previous.patch"
        patch_path.write_text(patch)
        outcome = "applied"
        if patch:
            try:
                await git(destination, "apply", "--3way", str(patch_path))
            except Conflict:
                # Preserve the full patch if Git cannot apply it. The coding job
                # must resolve the integration before submitting a new candidate.
                outcome = "needs_resolution"
        return {
            "integration": outcome,
            "previous_patch": str(patch_path),
            "previous_candidate": candidate.model_dump(mode="json"),
        }

    async def evidence(self, candidate: CandidateIdentity) -> Json:
        path = self.root / "candidates" / candidate.change_id / str(candidate.revision)
        if source_digest(path) != candidate.snapshot_sha256:
            raise Conflict("Accepted source was modified")
        return {
            "candidate": candidate.model_dump(mode="json"),
            "diff": await git(path, "diff", "--stat", candidate.base_commit, candidate.commit),
        }
