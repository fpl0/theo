"""Exercise archive attacks, source immutability and relocated runtime selection."""

import gzip
import hashlib
import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tarfile

import psutil
import pytest

from theo.backends.factory import backend_for
from theo.domain import AuthWait, Denied
from theo.maintenance import vm_bundle, vm_exports
from theo.maintenance.bundle_archive import extract
from theo.maintenance.bundles import Bundle, NativeSelection, inventory, native_settings
from theo.maintenance.source import IGNORED, files_digest
from theo.maintenance.vm_transfer import transfer


def archive_file(path, entries):
    with tarfile.open(path, "w:gz") as archive:
        for name, kind, body in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
            if kind == tarfile.REGTYPE:
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
            else:
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    member.linkname = body
                archive.addfile(member)


@pytest.mark.parametrize(
    "entries",
    [
        [("../outside", tarfile.REGTYPE, b"changed")],
        [("/outside", tarfile.REGTYPE, b"changed")],
        [("core/file", tarfile.REGTYPE, b"first"), ("core/file", tarfile.REGTYPE, b"second")],
        [("core/link", tarfile.LNKTYPE, "../outside")],
        [("core/link", tarfile.SYMTYPE, "../../outside")],
        [
            ("core/alias", tarfile.SYMTYPE, "../other"),
            ("core/alias/file", tarfile.REGTYPE, b"changed"),
        ],
        [("core/fifo", tarfile.FIFOTYPE, "")],
    ],
)
def test_bundle_archive_rejects_escapes_links_aliases_and_special_files(tmp_path, entries):
    outside = tmp_path / "outside"
    outside.write_bytes(b"unchanged")
    archive = tmp_path / "bundle.tar.gz"
    archive_file(archive, entries)
    with pytest.raises(Denied):
        extract(archive, tmp_path / "result")
    assert outside.read_bytes() == b"unchanged"


def test_bundle_archive_preserves_regular_bytes_and_internal_interpreter_links(tmp_path):
    archive = tmp_path / "bundle.tar.gz"
    archive_file(
        archive,
        [
            ("core", tarfile.DIRTYPE, ""),
            ("core/bin/python3", tarfile.REGTYPE, b"synthetic interpreter"),
            ("core/bin/python", tarfile.SYMTYPE, "python3"),
        ],
    )
    output = tmp_path / "result"
    previous = os.umask(0o077)
    try:
        extract(archive, output)
    finally:
        os.umask(previous)
    assert (output / "core/bin/python").read_bytes() == b"synthetic interpreter"
    assert (output / "core/bin/python").lstat().st_mode & 0o444 == 0o444
    assert (output / "core/bin/python3").stat().st_nlink == 1


def test_sealed_guest_interpreter_links_remain_readable_after_private_creation(
    tmp_path, monkeypatch
):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "python3").write_bytes(b"synthetic")
    previous = os.umask(0o077)
    try:
        (prefix / "python").symlink_to("python3")
    finally:
        os.umask(previous)
    monkeypatch.setattr(vm_exports.os, "chown", lambda *args, **kwargs: None)
    monkeypatch.setattr(vm_exports, "stopped", lambda: None)
    try:
        vm_exports.freeze(prefix)
        assert (prefix / "python").lstat().st_mode & 0o444 == 0o444
        assert (prefix / "python").read_bytes() == b"synthetic"
    finally:
        prefix.chmod(0o700)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACL permission regression")
def test_sealing_revokes_candidate_acl_write_grants(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    file = prefix / "module.py"
    file.write_text("synthetic = 1\n")
    username = pwd.getpwuid(os.getuid()).pw_name
    subprocess.run(["/bin/chmod", "+a", f"user:{username} allow write", str(file)], check=True)
    file.chmod(0o444)
    file.write_text("synthetic = 2\n")  # chmod alone preserves the extra grant.
    monkeypatch.setattr(vm_exports.os, "chown", lambda *args, **kwargs: None)
    monkeypatch.setattr(vm_exports, "stopped", lambda: None)
    try:
        vm_exports.freeze(prefix)
        with pytest.raises(PermissionError):
            file.write_text("synthetic = 3\n")
        assert file.read_text() == "synthetic = 2\n"
    finally:
        prefix.chmod(0o700)


@pytest.mark.parametrize("kind", [tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME])
def test_bundle_archive_rejects_huge_metadata_before_tarfile_allocates_it(tmp_path, kind):
    member = tarfile.TarInfo("extended-metadata")
    member.size = 1 << 40
    # TarInfo.tobuf normalizes the size of extended headers. Model the wire
    # attack directly, retaining the deliberately huge size and valid checksum.
    header = bytearray(member.tobuf(format=tarfile.GNU_FORMAT))
    header[156:157] = kind
    header[148:156] = b" " * 8
    header[148:156] = f"{sum(header):06o}\0 ".encode()
    archive = tmp_path / "metadata-bomb.tar.gz"
    archive.write_bytes(gzip.compress(header + b"\0" * 512))
    with pytest.raises(Denied, match="metadata"):
        extract(archive, tmp_path / "result")


def test_console_script_runs_after_its_original_prefix_is_removed(tmp_path):
    prefix = tmp_path / "original prefix"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/python").symlink_to(sys.executable)
    script = prefix / "bin/theo"
    script.write_text(f"#!{sys.executable}\nimport sys; print(repr(sys.argv[1:]))\n")
    script.chmod(0o755)
    vm_bundle.relocate_scripts(prefix)
    relocated = tmp_path / "relocated prefix"
    prefix.rename(relocated)
    result = subprocess.run(
        [str(relocated / "bin/theo"), "a b", "$(literal)"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['a b', '$(literal)']"
    assert not prefix.exists()


@pytest.mark.parametrize("mutation", [None, "edit", "add", "link"])
def test_final_guest_source_check_rejects_build_time_source_changes(
    tmp_path, monkeypatch, mutation
):
    source = tmp_path / "work/source"
    source.mkdir(parents=True)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    files = {"app.py": (b"value = 1\n", 0o644)}
    (source / "app.py").write_bytes(files["app.py"][0])
    (inputs / "manifest.json").write_text(
        json.dumps(
            {
                "source_files": {
                    name: [hashlib.sha256(body).hexdigest(), mode]
                    for name, (body, mode) in files.items()
                },
                "source_sha256": files_digest(files),
                "source_ignored": sorted(IGNORED),
            }
        )
    )
    monkeypatch.setattr(vm_exports, "ROOT", tmp_path)
    monkeypatch.setattr(vm_exports, "SOURCE", source)
    monkeypatch.setattr(vm_exports, "stopped", lambda: None)
    monkeypatch.setattr(vm_exports.os, "chown", lambda *args, **kwargs: None)
    if mutation == "edit":
        (source / "app.py").write_text("value = 2\n")
    elif mutation == "add":
        (source / "hidden.py").write_text("unexpected = True\n")
    elif mutation == "link":
        (source / "app.py").unlink()
        (source / "app.py").symlink_to(inputs / "manifest.json")
    try:
        if mutation:
            with pytest.raises(ValueError):
                vm_exports.source_check()
        else:
            assert vm_exports.source_check() == files_digest(files)
            assert vm_exports.source_check() == files_digest(files)
    finally:
        for path in [tmp_path, *tmp_path.rglob("*")]:
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


def test_native_bundle_requires_its_codex_helper_and_overrides_host_path(tmp_path, settings, db):
    native = tmp_path / "native"
    (native / "bin").mkdir(parents=True)
    executable = native / "bin/codex"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    (native / "runtime.json").write_text(
        NativeSelection(executables={"codex": "bin/codex"}).model_dump_json()
    )
    bundle = Bundle(
        bundle_id="synthetic",
        source_sha="a" * 40,
        tree="b" * 40,
        schema_min=1,
        schema_max=1,
        files=inventory(tmp_path),
        verification_hash="c" * 64,
    )
    with pytest.raises(Denied, match="code-mode host"):
        native_settings(tmp_path, bundle)
    helper = native / "bin/codex-code-mode-host"
    shutil.copy2(executable, helper)
    bundle = bundle.model_copy(update={"files": inventory(tmp_path)})
    paths, fingerprint = native_settings(tmp_path, bundle)
    selected = settings.model_copy(
        update={"bundle_native": paths, "bundle_native_fingerprint": fingerprint}
    )
    backend = backend_for("codex", db=db, settings=selected, binary="unrelated-host-codex")
    assert backend.binary == str(executable)
    with pytest.raises(AuthWait, match="does not contain"):
        backend_for("claude", db=db, settings=selected)
    assert "bundle_native" not in selected.model_dump()
    assert "bundle_native_fingerprint" not in selected.model_dump()


@pytest.mark.parametrize("fault", [None, "bytes", "oversized", "source", "extra", "exit"])
async def test_archive_stream_verifies_every_bounded_chunk_and_process_outcome(tmp_path, fault):
    data = b"synthetic bundle" * 30000
    receipt = {
        "archive_bytes": len(data),
        "archive_sha256": hashlib.sha256(data).hexdigest(),
        "source_sha256": "a" * 64,
    }
    child = tmp_path / "guest.py"
    child.write_text(
        "import base64,hashlib,json,sys\n"
        f"receipt = {receipt!r}\nfault = {fault!r}\n"
        "data = b'synthetic bundle' * 30000\n"
        "for line in sys.stdin:\n"
        " offset = int(line)\n"
        " body = data[offset:offset + (32769 if fault == 'oversized' else 32768)]\n"
        " response = {**receipt, 'offset':offset, "
        "'source_sha256': 'b'*64 if fault == 'source' else 'a'*64, "
        "'data_base64':base64.b64encode(b'changed' if fault == 'bytes' else body).decode(), "
        "'chunk_sha256':hashlib.sha256(body).hexdigest()}\n"
        " print(json.dumps(response),flush=True)\n"
        "if fault == 'extra': print('unsolicited',flush=True)\n"
        "sys.exit(1 if fault == 'exit' else 0)\n"
    )
    archive = tmp_path / "bundle.tar.gz"
    arguments = (
        [sys.executable, "-I", str(child)],
        dict(os.environ),
        tmp_path / "process.json",
        receipt,
        archive,
    )
    if fault:
        with pytest.raises(Denied):
            await transfer(*arguments)
    else:
        await transfer(*arguments)
        assert archive.read_bytes() == data
    identity = json.loads((tmp_path / "process.json").read_text())
    assert not psutil.pid_exists(identity["child_pid"])


async def test_archive_stream_deadline_stops_its_transport_process(tmp_path):
    receipt = {"archive_bytes": 1, "archive_sha256": "a" * 64, "source_sha256": "b" * 64}
    with pytest.raises((TimeoutError, Denied)):
        await transfer(
            [sys.executable, "-I", "-c", "import time;time.sleep(10)"],
            dict(os.environ),
            tmp_path / "process.json",
            receipt,
            tmp_path / "bundle.tar.gz",
            timeout=1,
        )
    identity = json.loads((tmp_path / "process.json").read_text())
    assert not psutil.pid_exists(identity["child_pid"])
