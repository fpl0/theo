"""Isolated source worktrees and fenced, serialized fast-forward promotion."""

import asyncio
from pathlib import Path

from theo.config import Settings
from theo.domain import Conflict, Denied, Json
from theo.isolation import launch_options
from theo.jobs import Jobs
from theo.storage import Database


async def git(repository: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repository),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(process.communicate(), 60)
    if process.returncode:
        raise Conflict("Git operation failed; inspect the isolated worktree")
    return stdout.decode().strip()


async def create_worktree(
    db: Database, owner: str, job_id: str, generation: int, repository: Path, destination: Path
) -> Json:
    await db.write(lambda connection: Jobs(db, owner).check(connection, job_id, generation))
    if destination.exists():
        actual = await git(destination, "rev-parse", "--show-toplevel")
        if Path(actual).resolve() != destination.resolve():
            raise Denied("Existing workspace is not the expected worktree")
    else:
        await git(
            repository, "worktree", "add", "-b", f"theo/job-{job_id}", str(destination), "HEAD"
        )
    return {
        "path": str(destination),
        "base_commit": await git(destination, "rev-parse", "HEAD"),
        "job_id": job_id,
    }


async def promote_worktree(
    db: Database,
    owner: str,
    job_id: str,
    generation: int,
    repository: Path,
    workspace: Path,
    expected_head: str,
) -> Json:
    jobs = Jobs(db, owner)
    resource = "git-promotion:" + str(repository.resolve())
    await jobs.resource(job_id, generation, resource)
    try:
        if await git(repository, "status", "--porcelain"):
            raise Conflict("Promotion target has unrelated changes")
        if await git(repository, "rev-parse", "HEAD") != expected_head:
            raise Conflict("Promotion target advanced; rebase and re-review")
        if await git(workspace, "status", "--porcelain"):
            raise Conflict("Commit and validate the isolated changes before promotion")
        head = await git(workspace, "rev-parse", "HEAD")
        await db.write(lambda connection: jobs.check(connection, job_id, generation))
        await git(repository, "merge", "--ff-only", head)
        return {"promoted_commit": head, "previous_commit": expected_head}
    finally:
        await db.execute(
            "DELETE FROM resource_claims WHERE resource=? AND job_id=? AND generation=?",
            (resource, job_id, generation),
        )


async def execute_scoped(
    settings: Settings, data_root: Path, workspace: Path, argv: list[str], timeout: int = 60
) -> Json:
    if not argv or len(argv) > 100 or any("\x00" in argument for argument in argv):
        raise ValueError("Invalid process arguments")
    from theo.backends.policy import worker_environment
    from theo.backends.process import stop_process

    if settings.worker_home is None:
        raise Denied("Configure a verified native runner before executing generated code")
    command, options = launch_options(settings, data_root, workspace, argv, generated=True)
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=workspace,
        env=worker_environment(settings.worker_home),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        **options,
    )
    assert process.stdout
    output = bytearray()
    try:
        async with asyncio.timeout(timeout):
            while chunk := await process.stdout.read(65536):
                output.extend(chunk)
                if len(output) > 1024 * 1024:
                    raise ValueError("Command output exceeded limit")
            await process.wait()
        return {"exit_code": process.returncode, "output": output.decode(errors="replace")}
    finally:
        await stop_process(process)
