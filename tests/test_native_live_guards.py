"""Offline checks that native subscription runs cannot accidentally reach CI."""

import asyncio
import os
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "script", ["native_e2e.py", "mcp_shim_probe.py", "complex_e2e.py", "evaluate_behaviour.py"]
)
@pytest.mark.parametrize("guard", ["CI", "GITHUB_ACTIONS", "THEO_TEST_OFFLINE", "missing_opt_in"])
async def test_native_script_refuses_before_starting_a_runtime(tmp_path, script, guard):
    environment = {**os.environ, "CI": "", "GITHUB_ACTIONS": "", "THEO_TEST_OFFLINE": ""}
    environment["PATH"] = str(tmp_path)  # Neither native CLI can be found or launched.
    if guard != "missing_opt_in":
        environment[guard] = "true" if guard != "THEO_TEST_OFFLINE" else "1"
    output = tmp_path / "must-not-exist.json"
    command = [
        sys.executable,
        str(Path(__file__).parents[1] / "scripts" / script),
        "--backend",
        "codex",
        "--model",
        "offline-fixture",
        "--output",
        str(output),
    ]
    if guard != "missing_opt_in":
        command.append("--live")
    if script == "evaluate_behaviour.py":
        command.extend(["--data-root", str(tmp_path / "must-not-exist")])
    process = await asyncio.create_subprocess_exec(
        *command,
        env=environment,
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, error = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode != 0
    assert (b"--live" if guard == "missing_opt_in" else b"local-only") in error
    assert not output.exists()


@pytest.mark.parametrize("guard", ["CI", "GITHUB_ACTIONS", "THEO_TEST_OFFLINE", "missing_opt_in"])
async def test_telegram_fault_probe_refuses_before_reading_credentials(tmp_path, guard):
    environment = {**os.environ, "CI": "", "GITHUB_ACTIONS": "", "THEO_TEST_OFFLINE": ""}
    if guard != "missing_opt_in":
        environment[guard] = "1"
    command = [
        sys.executable,
        str(Path(__file__).parents[1] / "scripts/telegram_uncertainty_probe.py"),
        "--data-root",
        str(tmp_path / "absent-root"),
        "--token-file",
        str(tmp_path / "absent-token"),
        "--bot",
        "fixture_bot",
        "--chat-id",
        "123",
        "--tag",
        "offline",
    ]
    if guard != "missing_opt_in":
        command.append("--live")
    process = await asyncio.create_subprocess_exec(
        *command, env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, error = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode != 0
    assert b"refuses offline/CI execution" in error
    assert not (tmp_path / "absent-root").exists()
