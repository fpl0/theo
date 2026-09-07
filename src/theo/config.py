"""Operator-owned configuration. Provider credentials are never stored here."""

import json
import os
import sys
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from theo.domain import StrictModel


def default_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Theo"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "theo"


class Settings(StrictModel):
    name: str = "Theo"
    owner_id: str = "owner"
    timezone: str = "Europe/Dublin"
    telegram_owner_id: int | None = None
    telegram_chat_id: int | None = None
    primary_backend: Literal["claude", "codex", "cursor", "grok"] | None = None
    primary_model: str | None = None
    max_runs: int = Field(default=2, ge=2, le=8)
    max_background: int = Field(default=1, ge=1, le=4)
    deadline_seconds: int = Field(default=1800, ge=60, le=7200)
    deep_deadline_seconds: int = Field(default=5400, ge=60, le=10800)
    inline_blob_limit: int = Field(default=16 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024)
    max_media_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    context_window: int = Field(default=32000, ge=8000)
    autonomous_hour_cap: int = 8
    autonomous_day_cap: int = 12
    urgent_hour_cap: int = 2
    quiet_start: int = Field(default=22, ge=0, le=23)
    quiet_end: int = Field(default=8, ge=0, le=23)
    worker_home: Path | None = None
    worker_python: Path | None = None
    runner_uid: int | None = None
    runner_gid: int | None = None
    isolation_verified: bool = False
    encrypted_storage_verified: bool = False
    qualified_backends: tuple[str, ...] = ()
    soak_completed: bool = False

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value


def load_settings(root: Path) -> Settings:
    path = root / "config.json"
    return Settings.model_validate_json(path.read_text()) if path.exists() else Settings()


def save_settings(root: Path, settings: Settings) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "config.json"
    tmp = root / "config.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(settings.model_dump(mode="json"), indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)
