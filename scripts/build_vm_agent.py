"""Build the pinned Tart guest agent with host-peer-only command admission.

This installation recipe handles public vendor source, never a maintenance
candidate. The resulting binary is for a disposable guest, not the physical host.
Its source, patch, toolchain and artifact hashes are retained beside the build.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

COMMIT = "0540136b95fcafac66f2c9a507178ae62502919b"
ARCHIVE_SHA256 = "1aba7eeaa44b00cc7cd61995c434b190815903d148e32aed55b80912168e2a01"
SOURCE_URL = "https://api.github.com/repos/openai/tart-guest-agent/tarball/" + COMMIT
GO_VERSION = "go version go1.27.1 darwin/arm64"


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--go", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or not args.go.is_absolute():
        parser.error("Use a new output directory and an absolute pinned Go executable")
    output.mkdir(mode=0o700, parents=True)
    home = output / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "PATH": str(args.go.parent) + ":/usr/bin:/bin:/usr/sbin:/sbin",
        "GOTOOLCHAIN": "local",
        "GOTELEMETRY": "off",
        "GOOS": "darwin",
        "GOARCH": "arm64",
        "CGO_ENABLED": "1",
        "CC": "/usr/bin/clang",
        "MACOSX_DEPLOYMENT_TARGET": "15.0",
        "GOPROXY": "https://proxy.golang.org",
        "GOSUMDB": "sum.golang.org",
        "GOMODCACHE": str(output / "modules"),
        "GOCACHE": str(output / "go-cache"),
    }
    version = subprocess.check_output(
        [str(args.go), "version"], env=environment, text=True, timeout=10
    ).strip()
    if version != GO_VERSION:
        parser.error("This installation recipe requires " + GO_VERSION)
    archive = output / "upstream.tar.gz"
    request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "Theo-VM-Setup"})
    with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as stream:
        total = 0
        while chunk := response.read(65536):
            total += len(chunk)
            if total > 10 * 1024 * 1024:
                raise ValueError("Vendor source archive exceeds its bound")
            stream.write(chunk)
    if sha(archive) != ARCHIVE_SHA256:
        raise ValueError("Vendor source archive does not match the pinned commit artifact")
    source = output / "source"
    source.mkdir()
    with tarfile.open(archive) as contents:
        for member in contents:
            parts = Path(member.name).parts
            if len(parts) < 2:
                continue
            if (
                member.name.startswith("/")
                or ".." in parts
                or not (member.isdir() or member.isfile())
            ):
                raise ValueError("Unexpected vendor archive entry")
            member.name = str(Path(*parts[1:]))
            contents.extract(member, source, filter="data")
    patch = Path(__file__).resolve().parents[1] / (
        "src/theo/maintenance/vendor/tart-guest-host-only.patch"
    )
    shutil.copyfile(patch, output / patch.name)
    binary = output / "tart-guest-agent"
    commands = [
        ["/usr/bin/git", "apply", "--no-index", "--unidiff-zero", str(patch)],
        [str(args.go), "mod", "download"],
        [str(args.go), "mod", "verify"],
        [
            str(args.go),
            "build",
            "-mod=readonly",
            "-trimpath",
            "-ldflags",
            "-s -w "
            "-X github.com/cirruslabs/tart-guest-agent/internal/version.Version=0.14.2-theo-host-only "
            "-X github.com/cirruslabs/tart-guest-agent/internal/version.Commit=" + COMMIT,
            "-o",
            str(binary),
            "./cmd",
        ],
        [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            "-",
            "--identifier",
            "theo.tart-guest-agent",
            "--timestamp=none",
            str(binary),
        ],
        ["/usr/bin/codesign", "--verify", "--strict", str(binary)],
    ]
    lock_hash = sha(source / "go.sum")
    with (output / "build.log").open("wb") as log:
        for command in commands:
            subprocess.run(
                command,
                cwd=source,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                check=True,
                timeout=900,
            )
    if sha(source / "go.sum") != lock_hash:
        raise ValueError("The vendor dependency lock changed during build")
    receipt = {
        "upstream_commit": COMMIT,
        "archive_sha256": ARCHIVE_SHA256,
        "patch_sha256": sha(patch),
        "go_version": version,
        "go_sha256": sha(args.go),
        "go_sum_sha256": lock_hash,
        "binary_sha256": sha(binary),
        "guest_acceptance": "pending; build success does not prove peer rejection",
    }
    with (output / "manifest.json").open("w") as stream:
        json.dump(receipt, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
