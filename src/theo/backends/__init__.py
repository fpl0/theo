"""Native subscription transports. No model provider API client is used."""

from theo.backends.native import ACPBackend, ClaudeBackend, CodexBackend, NativeBackend
from theo.config import Settings
from theo.storage import Database


def backend_for(
    name: str, *, db: Database, settings: Settings, binary: str | None = None
) -> NativeBackend:
    if name == "claude":
        return ClaudeBackend(db, settings, binary)
    if name == "codex":
        return CodexBackend(db, settings, binary)
    if name in ("cursor", "grok"):
        return ACPBackend(name, db, settings, binary)
    raise ValueError("Unsupported backend")
