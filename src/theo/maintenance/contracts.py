"""Versioned controller requests and immutable source/lease identities.

Requests are constructed by the core from an authenticated job, never forwarded
as arbitrary model-supplied owner, path, repository or generation fields.
"""

from typing import Annotated, Literal

from pydantic import Field

from theo.domain import StrictModel, WorkOrigin

type Identity = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")]
type Commit = Annotated[str, Field(pattern=r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$")]
type Stage = Literal[
    "preparing",
    "editing",
    "verifying",
    "publishing",
    "awaiting_ci",
    "merging",
    "packaging",
    "staged",
    "draining",
    "activating",
    "checking",
    "observing",
    "published",
    "deployed",
    "rolled_back",
    "failed",
    "cancelled",
]
type ControllerStatus = Literal[
    "queued", "working", "waiting", "blocked", "auth_wait", "quota_wait", "uncertain", "terminal"
]


class MaintenanceRequest(StrictModel):
    version: Literal[1] = 1
    request_id: Identity
    owner_id: Identity
    installation_id: Identity
    job_id: Identity
    review_job_id: Identity | None = None
    coding_job_id: Identity
    conversation_id: Identity
    origin: WorkOrigin
    objective: str = Field(min_length=1, max_length=4000)
    target: Literal["publish", "deploy"]
    evidence: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = Field(
        default=(), max_length=20
    )


class ControllerLease(StrictModel):
    change_id: Identity
    generation: int = Field(ge=1)
    revision: int = Field(ge=1)
    worker_id: Identity
    stage: Stage


class CandidateIdentity(StrictModel):
    change_id: Identity
    revision: int = Field(ge=1)
    base_commit: Commit
    commit: Commit
    tree: Commit
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    lock_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RoundJobs(StrictModel):
    revision: int = Field(ge=2)
    coding_job_id: Identity
    review_job_id: Identity


def round_effect(revision: int, name: str) -> str:
    """Keep original receipts readable; every later round has a new identity."""
    return name if revision == 1 else f"round-{revision}:{name}"
