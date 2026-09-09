"""Construct and verify operating-system boundaries for worker processes.

Builds macOS sandbox or dedicated-UID launch options from host configuration;
a workspace directory alone never counts as an isolation boundary.
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

from theo.config import Settings
from theo.domain import Denied, Json


def launch_options(
    settings: Settings,
    protected_root: Path,
    workspace: Path,
    command: list[str],
    *,
    generated: bool = False,
) -> tuple[list[str], Json]:
    if not settings.isolation_verified or settings.worker_home is None:
        raise Denied(
            "Native execution requires a verified isolated runner; run theo isolation verify"
        )
    if generated and sys.platform != "darwin":
        raise Denied(
            "Generated code requires the qualified Mac sandbox; a shared runner UID cannot protect native credentials"
        )
    if not generated and settings.runner_uid is not None and settings.runner_uid != os.getuid():
        if os.geteuid() != 0:
            raise Denied("Dedicated runner must be launched by the configured OS service")
        return command, {
            "user": settings.runner_uid,
            "group": settings.runner_gid,
            "extra_groups": [],
        }
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists():
        executable = Path(shutil.which(command[0]) or command[0]).resolve()
        profile = sandbox_profile(
            protected_root,
            settings.worker_home,
            workspace,
            generated=generated,
            runtime_executable=executable,
        )
        return ["/usr/bin/sandbox-exec", "-p", profile, str(executable), *command[1:]], {}
    raise Denied("No qualified OS execution boundary on this host")


def sandbox_profile(
    root: Path,
    worker_home: Path | None = None,
    workspace: Path | None = None,
    *,
    generated: bool = False,
    runtime_executable: Path | None = None,
) -> str:
    import json

    def quote(path: Path) -> str:
        return json.dumps(str(path.resolve()))

    runner = worker_home or root.parent / "runner"
    work = workspace or runner / "workspaces/probe"
    runtime_exception = (
        f" (require-not (literal {quote(runtime_executable)}))" if runtime_executable else ""
    )
    profile = (
        f"(version 1)(allow default)(deny file-read* file-write* (subpath {quote(root)}))"
        f"(deny file-read* (require-all (subpath {quote(Path.home())}) (require-not (subpath {quote(runner)})) (require-not (subpath {quote(Path(sys.prefix))})) (require-not (subpath {quote(Path(sys.base_prefix))})) (require-not (subpath {quote(Path(__file__).resolve().parents[2])})){runtime_exception}))"
        f"(allow file-read-metadata (literal {quote(Path.home())}))"
        f"(deny file-read* file-write* (require-all (subpath {quote(runner / 'workspaces')}) (require-not (subpath {quote(work)}))))"
        f'(deny file-write* (require-all (require-not (subpath {quote(runner)})) (require-not (literal "/dev/null"))))'
        '(deny process-exec (literal "/bin/launchctl") (literal "/bin/launchd") (literal "/usr/bin/security"))'
        "(deny mach-priv*)(deny process-info* (require-not (target self)))(deny signal)"
    )
    if generated:
        profile += f"(deny network*)(deny file-read* file-write* (require-all (subpath {quote(runner)}) (require-not (subpath {quote(work)}))))"
    # SQLite and native runtimes resolve every parent directory before opening
    # state files. Permit only ancestor metadata, never listing or file contents.
    for ancestor in work.resolve().parents:
        if not ancestor.is_relative_to(root.resolve()):
            profile += f"(allow file-read-metadata (literal {quote(ancestor)}))"
    return profile


async def verify_isolation(settings: Settings, root: Path) -> Json:
    probe = root / "isolation-canary"
    probe.write_text("protected")
    probe.chmod(0o600)
    code = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); denied=0\nfor op in (lambda:p.read_text(),lambda:p.write_text('changed')):\n try: op()\n except PermissionError: denied+=1\nsys.exit(0 if denied==2 else 1)"
    candidate = settings.model_copy(update={"isolation_verified": True})
    try:
        command, options = launch_options(
            candidate, root, root.parent, [sys.executable, "-c", code, str(probe)]
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=root.parent,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **options,
        )
        code_result = await asyncio.wait_for(process.wait(), 10)
        return {
            "verified": code_result == 0 and probe.read_text() == "protected",
            "test": "real process denied protected read and write",
            "platform": sys.platform,
        }
    except (Denied, TimeoutError, OSError) as exc:
        return {"verified": False, "reason": type(exc).__name__}
    finally:
        probe.unlink(missing_ok=True)
