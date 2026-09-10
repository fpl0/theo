"""Validate installation wiring and generate independent launchd service files.

Service loading and OS account/ACL provisioning remain explicit installation
operations. Application updates never rewrite these pinned service definitions.
"""

import argparse
import asyncio
import json
import os
import plistlib
import sys
from pathlib import Path

from theo.config import load_settings
from theo.domain import Denied, Json
from theo.maintenance.configuration import ControllerConfig, read_protected
from theo.maintenance.host import HostConfig
from theo.maintenance.policy import load_policy
from theo.maintenance.rpc import Client


def service_definition(module: str, executable: Path, config: Path, logs: Path) -> bytes:
    if (
        module not in ("controller", "host")
        or not executable.is_absolute()
        or not config.is_absolute()
    ):
        raise Denied("Service definitions require pinned absolute installation paths")
    label = "local.theo.maintenance." + module
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": [
                str(executable),
                "-m",
                "theo.maintenance." + module,
                "--config",
                str(config),
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "ProcessType": "Background",
            "StandardOutPath": str(logs / (module + ".stdout.log")),
            "StandardErrorPath": str(logs / (module + ".stderr.log")),
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        }
    )


async def check(config: ControllerConfig, host: HostConfig) -> Json:
    policy = load_policy(config.policy)
    core = load_settings(host.root)
    if not config.package_checks:
        raise Denied("Installed-package verification cannot be disabled for an installation")
    if (
        config.root.stat().st_uid != host.controller_uid
        or host.controller_uid in (0, config.core_uid)
        or host.root.stat().st_uid != config.core_uid
        or config.root.stat().st_mode & 0o077
    ):
        raise Denied("Controller state must be private to a separate non-root identity")
    if config.github_key_file and (
        config.github_key_file.stat().st_uid != host.controller_uid
        or config.github_key_file.stat().st_mode & 0o077
    ):
        raise Denied("GitHub App key must be private to the controller identity")
    if os.geteuid() not in (0, config.core_uid, host.controller_uid):
        raise Denied("Installation inspection requires a configured service identity")
    if (
        not core.worker_home
        or (core.worker_home / "workspaces").resolve() != config.workspaces.resolve()
        or core.maintenance_socket != config.socket
        or core.maintenance_token_file != config.token_file
        or core.maintenance_installation_id != policy.installation_id
    ):
        raise Denied(
            "Core workspace and maintenance endpoint settings must match this installation"
        )
    if config.host_socket != host.socket or config.host_token_file != host.token_file:
        raise Denied("Controller and host endpoints do not match")
    if (
        host.bundles.resolve() != (config.root / "bundles").resolve()
        or host.policy != config.policy
    ):
        raise Denied("Host bundle storage and standing policy must match the controller")
    for path in (config.token_file, config.host_token_file):
        if len(read_protected(path).strip()) < 32 or path.stat().st_mode & 0o007:
            raise Denied("Protected, distinct RPC credentials must be provisioned")
    if config.token_file.read_bytes() == config.host_token_file.read_bytes():
        raise Denied("Core and host endpoints require distinct credentials")
    status = await Client(config.host_socket, config.host_token_file).call("status", {})
    return {
        "installation_id": policy.installation_id,
        "repository": policy.repository,
        "policy_enabled": policy.enabled,
        "host": status,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller-config", type=Path, required=True)
    parser.add_argument("--host-config", type=Path, required=True)
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("check")
    services = sub.add_parser("services")
    services.add_argument("--destination", type=Path, required=True)
    services.add_argument("--python", type=Path, default=Path(sys.executable))
    retry = sub.add_parser("retry")
    retry.add_argument("change_id")
    retry.add_argument("--reason", required=True)
    args = parser.parse_args()
    config = ControllerConfig.model_validate_json(read_protected(args.controller_config))
    host = HostConfig.model_validate_json(read_protected(args.host_config))
    if args.operation == "services":
        args.destination.mkdir(parents=True, exist_ok=True)
        for module, path in (("controller", args.controller_config), ("host", args.host_config)):
            (args.destination / ("local.theo.maintenance." + module + ".plist")).write_bytes(
                service_definition(module, args.python, path.resolve(), args.destination)
            )
        print(json.dumps({"generated": str(args.destination), "loaded": False}))
    elif args.operation == "retry":
        result = asyncio.run(
            Client(config.socket, config.token_file).call(
                "signal",
                {"change_id": args.change_id, "name": "retry", "body": {"reason": args.reason}},
            )
        )
        print(json.dumps(result))
    else:
        print(json.dumps(asyncio.run(check(config, host))))


if __name__ == "__main__":
    main()
