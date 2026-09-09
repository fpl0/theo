"""Install and run the native macOS deployment services from a private manifest.

The manifest contains paths, never tokens. Credentials are read only by service
processes from an owner-only JSON environment file, not embedded in launchd plists.
"""

import argparse
import json
import logging
import os
import plistlib
import signal
import stat
import subprocess
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from theo.observability.budget import VM_MEMORY_GIB


def private_environment(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("Service environment must be owner-owned and mode 0600")
        value = json.load(stream)
    if not isinstance(value, dict) or any(not isinstance(v, str) for v in value.values()):
        raise ValueError("Service environment must contain string values")
    return value


def service_paths(manifest, kind):
    """Let monitoring advance independently of the core release and supervisor."""
    if kind in {"observer", "stack"}:
        return (
            manifest.get("observability_source", manifest["source"]),
            manifest.get("observability_python", manifest["python"]),
        )
    return manifest["source"], manifest["python"]


def definition(manifest, path, kind):
    root = Path(manifest["data_root"])
    source, python = service_paths(manifest, kind)
    return {
        "Label": "local.theo." + kind,
        "ProgramArguments": [
            python,
            str(Path(source) / "scripts/deploy_services.py"),
            "--manifest",
            str(path.resolve()),
            "run",
            kind,
        ],
        "WorkingDirectory": source,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "ExitTimeOut": 30,
        "Umask": 0o077,
        "StandardOutPath": str(root / (kind + ".bootstrap.log")),
        "StandardErrorPath": str(root / (kind + ".bootstrap.log")),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    }


def run(manifest, kind):
    source, python = service_paths(manifest, kind)
    env = {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin", **os.environ}
    env.update(private_environment(manifest["environment_file"]))
    env.setdefault("THEO_ALERT_HOST", os.uname().nodename)
    logger = logging.getLogger("deployment")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        Path(manifest["data_root"]) / (kind + ".log"), maxBytes=2_000_000, backupCount=2
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    stopped = False
    child = None

    def stop(signum, frame):
        nonlocal stopped
        stopped = True
        if child and child.poll() is None:
            child.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def command(argv, timeout=180):
        result = subprocess.run(
            argv, env=env, cwd=source, capture_output=True, text=True, timeout=timeout
        )
        # Command arguments and output may contain credential-bearing URLs.
        logger.info("operation=%s exit=%s", Path(argv[0]).name, result.returncode)
        if result.returncode:
            raise RuntimeError("Deployment dependency command failed: " + Path(argv[0]).name)

    if kind == "stack":
        while not stopped:
            try:
                ready = subprocess.run(
                    ["docker", "--context", "colima-theo-observability", "info"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
                if ready.returncode:
                    command(
                        [
                            "colima",
                            "start",
                            "theo-observability",
                            "--activate=false",
                            "--cpu",
                            "2",
                            "--memory",
                            str(VM_MEMORY_GIB),
                            "--disk",
                            "20",
                            "--vm-type",
                            "vz",
                        ]
                    )
                command(
                    [
                        "docker",
                        "--context",
                        "colima-theo-observability",
                        "compose",
                        "-f",
                        str(Path(source) / "observability/compose.yaml"),
                        "-f",
                        str(Path(manifest["data_root"]) / "observability-images.json"),
                        "up",
                        "-d",
                        "--remove-orphans",
                    ]
                )
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                logger.error("stack recovery failed: %s", type(exc).__name__)
            for _ in range(30):
                if stopped:
                    break
                time.sleep(1)
        return
    module = "theo.supervisor" if kind == "supervisor" else "theo.observer"
    child = subprocess.Popen(
        [python, "-m", module, "--data-root", manifest["data_root"]],
        env=env,
        cwd=source,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for line in child.stdout:
            # Native/application structured logging already excludes payloads.
            for key, value in env.items():
                if value and ("TOKEN" in key or "PASSWORD" in key or key == "THEO_HEARTBEAT_URL"):
                    line = line.replace(value, "[REDACTED]")
            logger.info("%s", line.rstrip())
        code = child.wait()
        if not stopped:
            raise SystemExit(code or 1)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        handler.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    sub = parser.add_subparsers(dest="operation", required=True)
    install = sub.add_parser("install")
    install.add_argument("--output", type=Path, required=True)
    install.add_argument(
        "--services",
        nargs="+",
        choices=("supervisor", "observer", "stack"),
        default=("supervisor", "observer", "stack"),
    )
    execute = sub.add_parser("run")
    execute.add_argument("kind", choices=("supervisor", "observer", "stack"))
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if args.operation == "install":
        args.output.mkdir(parents=True, exist_ok=True)
        for kind in args.services:
            target = args.output / ("local.theo." + kind + ".plist")
            target.write_bytes(plistlib.dumps(definition(manifest, args.manifest, kind)))
            target.chmod(0o600)
        print("Generated " + ", ".join(args.services) + " launchd definitions; not loaded")
    else:
        run(manifest, args.kind)


if __name__ == "__main__":
    main()
