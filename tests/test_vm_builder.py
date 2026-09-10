"""Host-side disconnected VM adapter checks with real Unix sockets and processes."""

import base64
import hashlib
import json
import os
import select
import socket
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from theo.maintenance import packet_sink, vm_guest

SINK = str(Path(packet_sink.__file__).resolve())
ARGUMENTS = [sys.executable, "-I", SINK, "--vm-fd", "0", "--vm-mac-address", "02:00:00:00:00:01"]


def test_vm_network_backend_discards_ipv4_ipv6_and_ethernet_without_reply():
    host, guest = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    process = subprocess.Popen(
        ARGUMENTS, stdin=guest, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    guest.close()
    try:
        assert process.stdout and select.select([process.stdout], [], [], 3)[0]
        assert process.stdout.readline() == b"theo-disconnected-network-ready\n"
        # Ethernet frames for ARP, IPv4, IPv6 and arbitrary data all enter the same sink.
        for kind in (b"\x08\x06", b"\x08\x00", b"\x86\xdd", b"\xff\xff"):
            host.send(b"\xff" * 6 + b"\x02\x00\x00\x00\x00\x01" + kind + b"synthetic" * 100)
        host.settimeout(0.4)
        with pytest.raises(TimeoutError):
            host.recv(65536)
        assert process.poll() is None
        process.terminate()
        out, error = process.communicate(timeout=3)
        assert process.returncode == 0 and out == error == b""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        host.close()


def test_vm_network_backend_refuses_a_real_network_socket():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        process = subprocess.run(ARGUMENTS, stdin=channel, capture_output=True, timeout=3)
    assert process.returncode != 0
    assert b"inherited Unix datagram socket" in process.stderr


def test_vm_network_backend_exits_when_its_driver_dies():
    parent_code = """
import json,os,subprocess,sys
child=subprocess.Popen(json.loads(sys.argv[1]),stdin=0,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
assert child.stdout.readline()==b'theo-disconnected-network-ready\\n'
print(child.pid,flush=True)
# The parent waits on a separate pipe, leaving fd 0 as the virtual network socket.
os.read(int(sys.argv[2]),1)
"""
    host, guest = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    read_fd, write_fd = os.pipe()
    parent = subprocess.Popen(
        [sys.executable, "-I", "-c", parent_code, json.dumps(ARGUMENTS), str(read_fd)],
        stdin=guest,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(read_fd,),
    )
    os.close(read_fd)
    guest.close()
    child = None
    try:
        assert parent.stdout
        assert select.select([parent.stdout], [], [], 3)[0]
        child = psutil.Process(int(parent.stdout.readline()))
        os.close(write_fd)
        write_fd = -1
        parent.wait(timeout=3)
        child.wait(timeout=3)
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    finally:
        if write_fd != -1:
            os.close(write_fd)
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=3)
        if child and child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
            child.kill()
        host.close()


def test_vm_guest_request_rejects_paths_types_and_unrecognized_authority():
    valid = {
        "argv": ["/usr/bin/true"],
        "cwd": "/private/var/theo-builder/work/source",
        "timeout": 30,
        "source_imports": False,
        "operation_id": "recipe-1",
    }
    assert vm_guest.request(json.dumps(valid).encode()).argv == ("/usr/bin/true",)
    for change in (
        {"cwd": "/private/var/theo-builder/work/../../tools"},
        {"cwd": "/Users/owner"},
        {"cwd": "/private/var/theo-builder/work\0"},
        {"argv": ["python", "--version"]},
        {"argv": ["/usr/bin/true\0"]},
        {"argv": ["/usr/bin/true", 42]},
        {"argv": []},
        {"timeout": True},
        {"timeout": 3601},
        {"source_imports": "true"},
        {"uid": 0},
        {"operation_id": "../../other"},
        {"operation_id": "x" * 81},
    ):
        with pytest.raises(ValueError):
            vm_guest.request(json.dumps(valid | change).encode())
    for malformed in (b"[]", b"null", b"{}", b" " * 32769):
        with pytest.raises(ValueError):
            vm_guest.request(malformed)


def test_vm_guest_environment_excludes_host_credentials(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/agent.sock")
    monkeypatch.setenv("PYTHONPATH", "/private/controller")
    command = vm_guest.Request(("/usr/bin/true",), vm_guest.WORK / "source", 3, False)
    environment = vm_guest.environment(command)
    assert "GITHUB_TOKEN" not in environment and "SSH_AUTH_SOCK" not in environment
    assert "PYTHONPATH" not in environment
    assert environment["UV_PYTHON_DOWNLOADS"] == "never"
    assert environment["THEO_TEST_OFFLINE"] == environment["UV_OFFLINE"] == "1"
    source = vm_guest.Request(command.argv, command.cwd, 3, True)
    assert vm_guest.environment(source)["PYTHONPATH"] == str(command.cwd / "src")


def test_vm_guest_refuses_execution_outside_a_root_mac_guest(monkeypatch):
    monkeypatch.setattr(vm_guest.os, "geteuid", lambda: 501)
    with pytest.raises(RuntimeError, match="disposable Mac guest"):
        vm_guest.check_guest()


def test_vm_result_replays_exact_effect_and_reads_bounded_verified_log(tmp_path, monkeypatch):
    monkeypatch.setattr(vm_guest, "result_root", lambda: tmp_path)
    effects = []
    output = b"synthetic output\n" * 5000

    def execute(command):
        effects.append(command)
        return {"exit_code": 0, "limit": None, "output_base64": base64.b64encode(output).decode()}

    monkeypatch.setattr(vm_guest, "execute", execute)
    command = vm_guest.Request(("/usr/bin/true",), vm_guest.WORK, 3, False, "recorded-1")
    first = vm_guest.run_recorded(command)
    assert vm_guest.read_result("recorded-1") == first
    assert vm_guest.run_recorded(command) == first and len(effects) == 1
    changed = vm_guest.Request(("/usr/bin/false",), command.cwd, 3, False, "recorded-1")
    with pytest.raises(ValueError, match="different request"):
        vm_guest.run_recorded(changed)
    assert len(effects) == 1
    restored = bytearray()
    while len(restored) < len(output):
        chunk = vm_guest.read_result("recorded-1", len(restored))
        data = base64.b64decode(chunk["data_base64"], validate=True)
        assert len(data) <= 32768
        assert chunk["chunk_sha256"] == hashlib.sha256(data).hexdigest()
        restored.extend(data)
    assert restored == output
    assert first["output_sha256"] == hashlib.sha256(restored).hexdigest()
    with pytest.raises(ValueError, match="offset"):
        vm_guest.read_result("recorded-1", -1)


def test_vm_result_crash_after_effect_is_not_replayed(tmp_path, monkeypatch):
    monkeypatch.setattr(vm_guest, "result_root", lambda: tmp_path)
    effects = []

    def execute(command):
        effects.append(command)
        return {"exit_code": 0, "limit": None, "output_base64": ""}

    write_record = vm_guest.write_record

    def interrupted_write(path, value):
        if path.suffix == ".json":
            raise OSError("synthetic process death before the result commit")
        write_record(path, value)

    monkeypatch.setattr(vm_guest, "execute", execute)
    monkeypatch.setattr(vm_guest, "write_record", interrupted_write)
    command = vm_guest.Request(("/usr/bin/true",), vm_guest.WORK, 3, False, "crash-1")
    with pytest.raises(OSError, match="process death"):
        vm_guest.run_recorded(command)
    monkeypatch.setattr(vm_guest, "write_record", write_record)
    with pytest.raises(RuntimeError, match="uncertain outcome"):
        vm_guest.run_recorded(command)
    assert len(effects) == 1
