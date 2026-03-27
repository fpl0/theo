"""Shared result types for the memory subsystem."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

type TrustTier = Literal[
    "owner", "owner_confirmed", "verified", "inferred", "external", "untrusted"
]
type SensitivityLevel = Literal["normal", "sensitive", "private"]
type EpisodeChannel = Literal["message", "email", "web", "observe", "cli", "internal"]
type EpisodeRole = Literal["user", "assistant", "tool", "system"]


@dataclasses.dataclass(frozen=True, slots=True)
class NodeResult:
    """A node retrieved from the knowledge graph."""

    id: int
    kind: str
    body: str
    trust: TrustTier
    confidence: float
    importance: float
    sensitivity: SensitivityLevel
    meta: dict[str, Any]
    created_at: datetime
    similarity: float | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class EpisodeResult:
    """An episode retrieved from episodic memory."""

    id: int
    session_id: UUID
    channel: EpisodeChannel
    role: EpisodeRole
    body: str
    trust: TrustTier
    importance: float
    sensitivity: SensitivityLevel
    meta: dict[str, Any]
    created_at: datetime
    similarity: float | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class EdgeResult:
    """An edge retrieved from the knowledge graph."""

    id: int
    source_id: int
    target_id: int
    label: str
    weight: float
    meta: dict[str, Any]
    valid_from: datetime
    valid_to: datetime | None
    created_at: datetime


@dataclasses.dataclass(frozen=True, slots=True)
class TraversalResult:
    """A node reached by graph traversal from a starting node."""

    node_id: int
    depth: int
    path: list[int]
    cumulative_weight: float
