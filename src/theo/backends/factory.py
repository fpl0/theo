"""Select a native adapter from the configured backend name.

The factory is used by the application and operator CLI. Individual protocol
modules can be imported without loading the other adapters.
"""

from theo.backends.acp import ACPBackend
from theo.backends.base import NativeBackend
from theo.backends.claude import ClaudeBackend
from theo.backends.codex import CodexBackend
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
