"""Versioned contracts. No provider SDK objects cross this boundary."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

type Json = dict[str, Any]
type Clock = Callable[[], float]


def now() -> float:
    return datetime.now(UTC).timestamp()


def uid() -> str:
    return str(uuid4())


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TheoError(Exception):
    code = "invalid_operation"
    retryable = False


class Conflict(TheoError):
    code = "revision_conflict"


class Denied(TheoError):
    code = "denied"


class Unavailable(TheoError):
    code = "unavailable"
    retryable = True


class QuotaWait(Unavailable):
    code = "waiting_for_quota"


class AuthWait(Unavailable):
    code = "waiting_for_auth"


class ProtocolError(TheoError):
    code = "protocol_error"


class Outcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    AUTH = "waiting_for_auth"
    QUOTA = "waiting_for_quota"
    UNCERTAIN = "uncertain"


class InputPart(StrictModel):
    kind: str
    text: str | None = None
    artifact_id: str | None = None
    metadata: Json = Field(default_factory=dict)


class BackendDescriptor(StrictModel):
    backend: str
    runtime_version: str
    fingerprint: str
    models: tuple[str, ...]
    capabilities: frozenset[str] = frozenset({"text", "tools"})


class SessionHandle(StrictModel):
    backend: str
    account: str
    workspace: str
    native_id: str
    canonical_sequence: int
    fingerprint: str


class ExecutionRequest(StrictModel):
    schema_version: int = 1
    run_id: str
    job_id: str
    conversation_id: str
    owner_id: str
    backend: str
    model: str
    lane: str
    context: str
    parts: tuple[InputPart, ...] = ()
    workspace: Path
    deadline: float
    generation: int
    tool_socket: str
    tool_token: str
    max_turns: int = 40


class ExecutionEvent(StrictModel):
    schema_version: int = 1
    event_id: str = Field(default_factory=uid)
    run_id: str
    sequence: int
    timestamp: float = Field(default_factory=now)
    kind: str
    payload: Json = Field(default_factory=dict)


class ExecutionOutcome(StrictModel):
    status: Outcome
    text: str = ""
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    session: SessionHandle | None = None


class ToolContext(StrictModel):
    owner_id: str
    conversation_id: str
    job_id: str
    run_id: str
    generation: int
    workspace: Path
    tools: frozenset[str]


class ToolResult(StrictModel):
    status: str
    data: Any = None
    evidence: tuple[str, ...] = ()
    action_id: str | None = None
    error: str | None = None
    retryable: bool = False
