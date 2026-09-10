"""Seal guest outputs and serve immutable, hash-checked archive chunks to the host.

Only the protected guest control agent invokes this script as root. Candidate
processes are stopped and their output is moved beneath a root-owned parent before
its permissions are frozen. No candidate Python or packaging hook runs here.
"""

import argparse
import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import cast

ROOT = Path("/private/var/theo-builder")
SOURCE = ROOT / "work/source"
BUNDLE = ROOT / "work/relocated-bundle"
# The locked wheels contain 31,234 files before interpreter files and directory
# entries; a development export contains two complete prefixes.
MAX_FILES = 100000
MAX_BYTES = 4_000_000_000
# Use the response size exercised by the pinned guest transport. Larger direct
# responses failed in qualification; verified recipe logs also use 32 KiB.
CHUNK_BYTES = 32 * 1024


def check_guest() -> None:
    if sys.platform != "darwin" or os.geteuid() != 0:
        raise RuntimeError("Export sealing requires the root Mac guest control agent")
    model = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], timeout=5)
    if not model.startswith(b"VirtualMac"):
        raise RuntimeError("Guest exports cannot run on a physical host")
    for path in (ROOT, Path(__file__).resolve()):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("Guest export authority must remain protected")


def stopped() -> None:
    result = subprocess.run(["/usr/bin/pgrep", "-u", "622"], capture_output=True, timeout=5)
    if result.returncode != 1:
        raise RuntimeError("Guest build processes remain; output cannot be sealed")


def freeze(path: Path) -> tuple[int, int]:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Guest output must be a real directory")
    count, size = 0, 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        parent = Path(directory)
        os.chown(parent, 0, 0)
        parent.chmod(0o555)
        for name in [*dirs, *files]:
            item = parent / name
            info = item.lstat()
            count += 1
            if count > MAX_FILES:
                raise ValueError("Guest output exceeds its entry limit")
            if info.st_mode & 0o7000:
                raise ValueError("Guest output contains a privileged file mode")
            if stat.S_ISLNK(info.st_mode):
                link = os.readlink(item)
                if Path(link).is_absolute() or not item.resolve(strict=True).is_relative_to(
                    path.resolve()
                ):
                    raise ValueError("Guest output contains an external link")
                os.chown(item, 0, 0, follow_symlinks=False)
                # macOS checks symlink read permissions for readlink(). A link
                # created under the build UID's private umask must remain
                # readable after root takes ownership of the sealed runtime.
                if os.chmod in os.supports_follow_symlinks:
                    os.chmod(item, 0o755, follow_symlinks=False)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1 or info.st_size > 1_000_000_000:
                    raise ValueError("Guest output contains a linked or oversized file")
                size += info.st_size
                if size > MAX_BYTES:
                    raise ValueError("Guest output exceeds its byte limit")
                os.chown(item, 0, 0)
                item.chmod(0o555 if info.st_mode & 0o111 else 0o444)
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError("Guest output contains a special file")
    if sys.platform == "darwin":
        # POSIX read-only modes do not revoke macOS ACL write grants left by
        # candidate code. Remove them after validating the complete tree; -P
        # prevents traversal through symbolic links.
        subprocess.run(
            ["/bin/chmod", "-R", "-P", "-N", str(path)],
            check=True,
            capture_output=True,
            timeout=60,
        )
    stopped()
    return count, size


def source_check() -> str:
    manifest = cast(dict[str, object], json.loads((ROOT / "inputs/manifest.json").read_bytes()))
    expected = cast(dict[str, list[object]], manifest["source_files"])
    ignored = set(cast(list[str], manifest["source_ignored"]))
    checked = ROOT / "checked-source"
    stopped()
    if not checked.exists():
        if SOURCE.is_symlink():
            raise ValueError("Candidate source was replaced by a link")
        SOURCE.rename(checked)
        freeze(checked)
    actual: dict[str, list[object]] = {}
    for directory, dirs, names in os.walk(checked, followlinks=False):
        dirs[:] = [name for name in dirs if name not in ignored]
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ValueError("Candidate source contains a new directory link")
        for name in names:
            if name in ignored or name.endswith(".pyc"):
                continue
            item = Path(directory) / name
            info = item.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Candidate source was replaced by a linked file")
            with item.open("rb") as stream:
                sha = hashlib.file_digest(stream, "sha256").hexdigest()
            actual[str(item.relative_to(checked))] = [sha, 0o755 if info.st_mode & 0o111 else 0o644]
    if actual != expected:
        raise ValueError("Candidate source changed while its verification or build was running")
    encoded = json.dumps(actual, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sha = hashlib.sha256(encoded.encode()).hexdigest()
    if sha != manifest["source_sha256"]:
        raise ValueError("Source receipt differs from the accepted host snapshot")
    return sha


def seal() -> dict[str, object]:
    exports = ROOT / "exports"
    exports.mkdir(mode=0o700, exist_ok=True)
    receipt = exports / "bundle.json"
    if receipt.exists():
        return cast(dict[str, object], json.loads(receipt.read_bytes()))
    source_sha = source_check()
    sealed = ROOT / "sealed-bundle"
    if BUNDLE.is_symlink() or not BUNDLE.is_dir():
        raise ValueError("Expected a complete relocated application bundle")
    BUNDLE.rename(sealed)
    count, size = freeze(sealed)
    output = exports / "bundle.tar.gz"
    with output.open("xb") as stream:
        with tarfile.open(fileobj=stream, mode="w:gz", compresslevel=3) as archive:
            for path in sorted(sealed.rglob("*")):
                archive.add(
                    path,
                    arcname=str(path.relative_to(sealed)),
                    recursive=False,
                    filter=archive_metadata,
                )
        stream.flush()
        os.fsync(stream.fileno())
    output.chmod(0o400)
    with output.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    result: dict[str, object] = {
        "archive_sha256": sha,
        "archive_bytes": output.stat().st_size,
        "source_sha256": source_sha,
        "file_count": count,
        "unpacked_bytes": size,
    }
    with receipt.open("x") as stream:
        json.dump(result, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return result


def archive_metadata(member: tarfile.TarInfo) -> tarfile.TarInfo:
    """Omit host identities and per-file timestamp extension records."""
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    member.mtime = 0
    member.pax_headers = {}
    return member


def read(offset: int) -> dict[str, object]:
    receipt = cast(dict[str, object], json.loads((ROOT / "exports/bundle.json").read_bytes()))
    if not 0 <= offset < int(str(receipt["archive_bytes"])):
        raise ValueError("Export offset exceeds the sealed archive")
    with (ROOT / "exports/bundle.tar.gz").open("rb") as stream:
        stream.seek(offset)
        body = stream.read(CHUNK_BYTES)
    return {
        **receipt,
        "offset": offset,
        "data_base64": base64.b64encode(body).decode("ascii"),
        "chunk_sha256": hashlib.sha256(body).hexdigest(),
    }


def stream() -> None:
    """Answer one offset at a time so every response stays within transport bounds."""
    while line := sys.stdin.buffer.readline(81):
        offset = int(line)
        if len(line) > 80 or line != f"{offset}\n".encode():
            raise ValueError("Invalid archive stream offset")
        print(json.dumps(read(offset)), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("source", "seal", "read", "stream"))
    parser.add_argument("--offset", type=int)
    args = parser.parse_args()
    check_guest()
    if args.operation == "stream":
        stream()
        return
    if args.operation == "source":
        result = {"source_sha256": source_check()}
    elif args.operation == "seal":
        result = seal()
    else:
        if args.offset is None:
            parser.error("An export read requires its offset")
        result = read(args.offset)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
