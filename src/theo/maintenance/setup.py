"""Validate installation wiring and generate independent launchd service files.

Service loading and OS account/ACL provisioning remain explicit installation
operations. Application updates never rewrite these pinned service definitions.
"""

import argparse
import asyncio
import json
import os
import plistlib
import pwd
import sys
from pathlib import Path

from theo.config import load_settings
from theo.domain import Denied, Json
from theo.maintenance.configuration import (
    ControllerConfig,
    read_operator_file,
    read_protected,
    require_root_parents,
)
from theo.maintenance.host import HostConfig
from theo.maintenance.minimum import acceptance_files
from theo.maintenance.policy import load_policy
from theo.maintenance.rpc import Client


def service_definition(
    module: str,
    executable: Path,
    config: Path,
    logs: Path,
    *,
    service_uid: int | None = None,
) -> bytes:
    if (
        module not in ("controller", "host")
        or not executable.is_absolute()
        or not config.is_absolute()
    ):
        raise Denied("Service definitions require pinned absolute installation paths")
    label = "local.theo.maintenance." + module
    account = pwd.getpwuid(os.geteuid() if service_uid is None else service_uid)
    return plistlib.dumps(
        {
            "Label": label,
            "UserName": account.pw_name,
            "ProgramArguments": [
                str(executable),
                "-I",
                "-B",
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
    if config.policy_uid != 0:
        raise Denied("Installation policy ownership must be pinned to root")
    read_operator_file(config.policy)
    if not config.minimum_source or not config.minimum_source_sha256:
        raise Denied("An installation requires its protected minimum verification source")
    acceptance_files(config.minimum_source, config.minimum_source_sha256, {})
    policy = load_policy(config.policy, expected_uid=0)
    core = load_settings(host.root)
    if (host.core_uid, host.core_gid) != (config.core_uid, config.core_gid):
        raise Denied("Controller and supervisor must agree on the core service identity")
    if not host.core_uid or not host.core_gid or host.selection is None:
        raise Denied("Install a non-root core and an independently protected runtime selection")
    if not host.selection.is_absolute() or any(
        host.selection.resolve().is_relative_to(path.resolve())
        or host.selection.parent.resolve().is_relative_to(path.resolve())
        or host.state_root.resolve().is_relative_to(path.resolve())
        for path in (host.root, config.root, config.workspaces)
    ):
        raise Denied("Recovery authority must remain outside core, controller and job storage")
    for path in (host.state_root, host.selection.parent):
        require_root_parents(path)
        if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
            raise Denied("Supervisor state and selection directories must be root-owned")
    if host.state_root.stat().st_mode & 0o077:
        raise Denied("Supervisor process records must be private to its root service")
    if host.telegram_token_file and (
        host.telegram_token_file.is_symlink()
        or host.telegram_token_file.stat().st_uid != 0
        or host.telegram_token_file.stat().st_mode & 0o077
    ):
        raise Denied("The supervisor's Telegram credential must be root-private")
    if not config.package_checks or not config.vm:
        raise Denied("An installation requires VM isolation and installed-package verification")
    if not config.bundle_root:
        raise Denied("Install shared read-only bundles outside private controller state")
    if (
        config.bundles.is_symlink()
        or config.bundles.stat().st_uid != host.controller_uid
        or config.bundles.stat().st_gid != config.core_gid
        or config.bundles.stat().st_mode & 0o022
    ):
        raise Denied("Shared bundle storage must be controller-owned with read-only peer access")
    if (
        config.root.stat().st_uid != host.controller_uid
        or host.controller_uid in (0, config.core_uid)
        or pwd.getpwuid(host.controller_uid).pw_gid in (0, config.core_gid)
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
    if host.bundles.resolve() != config.bundles.resolve() or host.policy != config.policy:
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
    config = ControllerConfig.model_validate_json(read_operator_file(args.controller_config))
    host = HostConfig.model_validate_json(read_operator_file(args.host_config))
    if args.operation == "services":
        args.destination.mkdir(parents=True, exist_ok=True)
        for module, path in (("controller", args.controller_config), ("host", args.host_config)):
            (args.destination / ("local.theo.maintenance." + module + ".plist")).write_bytes(
                service_definition(
                    module,
                    args.python,
                    path.resolve(),
                    args.destination,
                    service_uid=host.controller_uid if module == "controller" else 0,
                )
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
