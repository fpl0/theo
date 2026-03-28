"""Typed, immutable event models for the Theo event bus."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

# Channel and role literals mirror the DB CHECK constraints in 0003.
type Channel = Literal["message", "email", "web", "observe", "cli", "internal"]
type Role = Literal["user", "assistant", "tool", "system"]
type TrustTier = Literal[
    "owner", "owner_confirmed", "verified", "inferred", "external", "untrusted"
]


class Event(BaseModel):
    """Base event — immutable, timestamped, optionally session-scoped."""

    model_config = ConfigDict(frozen=True)

    durable: ClassVar[bool] = True

    id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    session_id: UUID | None = None
    channel: Channel | None = None


# ── Durable events (persisted before dispatch) ──────────────────────


class MessageReceived(Event):
    """Incoming message from a gate."""

    body: str
    role: Role = "user"
    meta: dict[str, object] = {}
    trust: TrustTier = "owner"


class ResponseComplete(Event):
    """Fully assembled response ready for delivery."""

    body: str
    role: Role = "assistant"
    episode_id: int | None = None


class SystemEvent(Event):
    """Lifecycle or operational signal."""

    kind: Literal["startup", "shutdown", "error", "health"]
    detail: str = ""


# ── Ephemeral events (dispatch only, no persistence) ─────────────────


class ResponseChunk(Event):
    """Streaming text fragment — ephemeral, not persisted."""

    durable: ClassVar[bool] = False

    body: str
    done: bool = False


class BudgetWarning(Event):
    """Emitted when token usage crosses the warning threshold."""

    durable: ClassVar[bool] = False

    scope: Literal["daily", "session"]
    used_tokens: int
    cap_tokens: int
    usage_ratio: float
