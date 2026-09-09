"""Resolve the operator-provided Telegram token for daemon startup.

Uses the existing environment-first lookup and optional macOS Keychain fallback;
offline test mode always disables credential access.
"""

import os
import subprocess
import sys


def telegram_token(service: str = "theo.telegram") -> str | None:
    if os.environ.get("THEO_TEST_OFFLINE") == "1":
        return None
    token = os.environ.get("THEO_TELEGRAM_TOKEN")
    if token:
        return token
    if sys.platform == "darwin":
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    return None
