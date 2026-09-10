"""Prepare the trusted guest control layer before any candidate code is admitted.

The driver copies this pinned script and its hash-bound inputs into a fresh VM.
All paths are fixed guest paths. Public image credentials are invalidated, and
candidate source is extracted with the build identity rather than guest root.
"""

import argparse
import hashlib
import json
import os
import plistlib
import pwd
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import cast

ROOT = Path("/private/var/theo-builder")
INPUTS = ROOT / "inputs"
TOOLS = ROOT / "tools"
WORK = ROOT / "work"
UID = 622


def check_guest() -> None:
    if os.geteuid() != 0 or sys.platform != "darwin":
        raise RuntimeError("Bootstrap requires the disposable Mac guest")
    model = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], timeout=5)
    if not model.startswith(b"VirtualMac"):
        raise RuntimeError("Bootstrap is forbidden on a physical host")


def checked(*argv: str) -> None:
    result = subprocess.run(argv, capture_output=True, timeout=30)
    if result.returncode:
        # Some setup arguments are ephemeral guest passwords. Never include
        # argv or raw stderr in an exception that crosses the control stream.
        raise RuntimeError("Guest setup command failed: " + argv[0])


def verify_inputs() -> dict[str, object]:
    values = cast(dict[str, object], json.loads((INPUTS / "manifest.json").read_bytes()))
    checksums = cast(dict[str, str], values["files"])
    for name, expected in checksums.items():
        if Path(name).name != name:
            raise ValueError("Input names must be simple filenames")
        path = INPUTS / name
        if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
            raise ValueError("Guest input must be a regular private copy")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError("Guest input failed its host-recorded checksum")
    return values


def install_agent() -> None:
    verify_inputs()
    for name in (
        "tart-guest-agent",
        "uv",
        "vm_guest.py",
        "vm_bootstrap.py",
        "vm_bundle.py",
        "vm_exports.py",
    ):
        shutil.copyfile(INPUTS / name, TOOLS / name)
        (TOOLS / name).chmod(0o755 if name in ("tart-guest-agent", "uv") else 0o644)
    checked("/usr/bin/codesign", "--verify", "--strict", str(TOOLS / "tart-guest-agent"))
    target = Path("/Library/LaunchDaemons/local.theo.builder-agent.plist")
    target.write_bytes(
        plistlib.dumps(
            {
                "Label": "local.theo.builder-agent",
                "ProgramArguments": [str(TOOLS / "tart-guest-agent"), "--run-rpc"],
                "RunAtLoad": True,
                "KeepAlive": True,
                "ThrottleInterval": 2,
                "StandardOutPath": str(ROOT / "agent.log"),
                "StandardErrorPath": str(ROOT / "agent.log"),
                "EnvironmentVariables": {"HOME": "/var/root", "PATH": "/usr/bin:/bin"},
            }
        )
    )
    target.chmod(0o644)
    checked("/bin/launchctl", "bootstrap", "system", str(target))
    old = Path("/Library/LaunchAgents/org.cirruslabs.tart-guest-agent.plist")
    old.rename(ROOT / "original-agent.plist")
    admin = pwd.getpwnam("admin")
    # The old agent owns this RPC. Return the handover intent before stopping
    # it; the independent root daemon is already registered and retries its port.
    subprocess.Popen(
        [
            "/bin/sh",
            "-c",
            f"sleep 1; /bin/launchctl bootout gui/{admin.pw_uid}/org.cirruslabs.tart-guest-agent",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def prepare() -> dict[str, object]:
    values = verify_inputs()
    if any(account.pw_uid == UID or account.pw_name == "_theobuild" for account in pwd.getpwall()):
        raise RuntimeError("Build identity already exists; discard this non-fresh VM")
    # Images publish their login credentials. Disable the preconfigured accounts
    # before candidate execution; the control agent already runs as a root daemon.
    for account in pwd.getpwall():
        if account.pw_uid >= 500:
            checked("/usr/bin/pwpolicy", "-u", account.pw_name, "-disableuser")
            policy = subprocess.run(
                [
                    "/usr/bin/dscl",
                    ".",
                    "-authonly",
                    account.pw_name,
                    "theo-disabled-account-probe",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # The deprecated -getpolicy command itself fails for a disabled
            # account. Require the directory service's specific disabled-account
            # error; a generic bad-password result would not establish lockdown.
            if policy.returncode == 0 or "eDSAuthAccountDisabled" not in (
                policy.stdout + policy.stderr
            ):
                raise RuntimeError("A preconfigured guest login account is still enabled")
    for target in ("system/com.openssh.sshd", "system/com.apple.screensharing"):
        checked("/bin/launchctl", "disable", target)
        subprocess.run(["/bin/launchctl", "bootout", target], capture_output=True, timeout=10)
    for attribute, value in (
        ("UniqueID", str(UID)),
        ("PrimaryGroupID", "20"),
        ("UserShell", "/usr/bin/false"),
        ("NFSHomeDirectory", str(WORK / "home")),
        ("IsHidden", "1"),
    ):
        checked("/usr/bin/dscl", ".", "-create", "/Users/_theobuild", attribute, value)
    with tarfile.open(INPUTS / "wheels.tar") as archive:
        for member in archive:
            if not member.isfile() or Path(member.name).name != member.name:
                raise ValueError("The wheel input must contain only flat regular files")
            member.mode = 0o644
            archive.extract(member, TOOLS / "wheels", filter="data")
    for directory in (WORK, WORK / "home", WORK / "tmp", WORK / "cache", WORK / "source"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(directory, UID, 20)
    for path in TOOLS.rglob("*"):
        if path.is_symlink():
            if not path.resolve().is_relative_to(TOOLS):
                raise ValueError("Guest tooling links must remain within its protected directory")
            continue
        os.chown(path, 0, 0)
        path.chmod((path.stat().st_mode & 0o755) | (0o555 if path.is_dir() else 0o444))
    (TOOLS / "uv").chmod(0o755)
    for argv in (
        ["/usr/bin/sudo", "-n", "true"],
        ["/usr/bin/dscl", ".", "-authonly", "admin", "admin"],
    ):
        probe = subprocess.run(
            argv,
            user=UID,
            group=20,
            extra_groups=[],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
        if probe.returncode == 0:
            raise RuntimeError("The build identity still has an administrative authentication path")
    # Source is executable input. Even archive extraction runs under its UID.
    checked_source = subprocess.run(
        [
            str(TOOLS / "python/bin/python3"),
            "-I",
            "-c",
            "import tarfile; "
            f"t=tarfile.open({str(INPUTS / 'source.tar')!r}); "
            f"t.extractall({str(WORK / 'source')!r},filter='data'); t.close()",
        ],
        user=UID,
        group=20,
        extra_groups=[],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
    )
    if checked_source.returncode:
        raise RuntimeError("Candidate source extraction failed in the build identity")
    receipt = {
        "ready": True,
        "guest_uid": UID,
        "public_image_credentials_revoked": True,
        "source_sha256": values["source_sha256"],
    }
    (ROOT / "prepared.json").write_text(json.dumps(receipt))
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("agent", "prepare"))
    args = parser.parse_args()
    check_guest()
    if args.phase == "agent":
        install_agent()
        print(json.dumps({"handover_requested": True}), flush=True)
    else:
        print(json.dumps(prepare()), flush=True)


if __name__ == "__main__":
    main()
