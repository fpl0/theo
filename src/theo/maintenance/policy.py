"""Versioned standing maintenance authority owned by the installation operator.

The controller reads this protected file afresh before effects. Candidate code,
memory, tool arguments and remote repository content cannot widen its authority.
"""

import os
import stat
from pathlib import Path

from pydantic import Field, field_validator

from theo.domain import Denied, StrictModel, digest
from theo.maintenance.contracts import Identity, MaintenanceRequest


class MaintenancePolicy(StrictModel):
    version: int = Field(default=1, ge=1, le=1)
    revision: int = Field(ge=1)
    owner_id: Identity
    installation_id: Identity
    repository_id: int = Field(gt=0)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_branch: str = Field(default="main", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_./-]{0,100}$")
    enabled: bool = False
    allow_publish: bool = True
    allow_deploy: bool = True
    allow_proactive: bool = False
    max_candidate_revisions: int = Field(default=2, ge=1, le=10)
    proactive_deployments_per_day: int = Field(default=2, ge=1, le=10)
    drain_seconds: int = Field(default=120, ge=10, le=600)
    startup_seconds: int = Field(default=120, ge=10, le=600)
    probation_seconds: int = Field(default=600, ge=60, le=3600)
    operation_seconds: int = Field(default=14400, ge=300, le=86400)

    @field_validator("base_branch")
    @classmethod
    def valid_branch(cls, value: str) -> str:
        if (
            ".." in value
            or "//" in value
            or any(
                component.startswith(".") or component.endswith((".", ".lock")) or not component
                for component in value.split("/")
            )
        ):
            raise ValueError("Invalid Git branch")
        return value

    @property
    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))

    def authorize(self, request: MaintenanceRequest) -> None:
        if request.owner_id != self.owner_id or request.installation_id != self.installation_id:
            raise Denied("Maintenance request belongs to a different owner or installation")
        if not self.enabled:
            raise Denied("Maintenance is not enabled by installation policy")
        if not self.allow_publish or (request.target == "deploy" and not self.allow_deploy):
            raise Denied("Maintenance target is outside standing permission")
        if request.origin == "system" or (
            request.origin == "autonomous" and (not self.allow_proactive or not request.evidence)
        ):
            raise Denied("Proactive maintenance requires standing permission and source evidence")


def load_policy(path: Path, *, expected_uid: int | None = None) -> MaintenancePolicy:
    """Read one bounded, non-symlink policy with restrictive OS ownership."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Denied("Maintenance policy is unavailable or is a symlink") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o022
            or metadata.st_size > 65536
            or (expected_uid is not None and metadata.st_uid != expected_uid)
        ):
            raise Denied("Maintenance policy must be a protected, operator-owned regular file")
        with os.fdopen(descriptor, "r", closefd=False) as stream:
            value = stream.read(65537)
        if len(value.encode()) > 65536:
            raise Denied("Maintenance policy exceeds the size limit")
        return MaintenancePolicy.model_validate_json(value)
    finally:
        os.close(descriptor)
