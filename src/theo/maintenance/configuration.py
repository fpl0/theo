"""Protected installation wiring for the independently pinned controller.

Paths and verification commands are operator configuration, never tool arguments.
The candidate cannot choose a repository, service, executable or acceptance recipe.
"""

import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from theo.domain import Denied, StrictModel
from theo.maintenance.contracts import Commit
from theo.maintenance.vm_config import VmSettings


class CheckRecipe(StrictModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    argv: tuple[str, ...] = Field(min_length=1, max_length=100)
    timeout: int = Field(default=900, ge=1, le=3600)


class ControllerConfig(StrictModel):
    core_uid: int = Field(default_factory=os.geteuid, ge=0)
    core_gid: int = Field(default_factory=os.getegid, ge=0)
    root: Path
    bundle_root: Path | None = None
    policy: Path
    socket: Path
    token_file: Path
    host_socket: Path
    host_token_file: Path
    workspaces: Path
    installed_source: Commit
    builder_python: Path
    uv: Path
    node: Path | None = None
    vm: VmSettings | None = None
    runtime_reads: tuple[Path, ...]
    checks: tuple[CheckRecipe, ...] = Field(min_length=1)
    package_checks: bool = True
    required_checks: dict[str, int] = Field(min_length=1)
    github_auth: Literal["github_app", "gh_cli"] = "github_app"
    github_cli: Path | None = None
    github_cli_config: Path | None = None
    github_app_id: int | None = Field(default=None, gt=0)
    github_installation_id: int | None = Field(default=None, gt=0)
    github_key_file: Path | None = None
    dependency_wheels: Path
    native_files: dict[str, Path] = Field(default_factory=dict)
    native_executables: dict[Literal["codex", "claude", "cursor", "grok"], str] = Field(
        default_factory=lambda: {"codex": "bin/codex"}
    )
    runtime_extras: tuple[Literal["browser", "embeddings", "speech"], ...] = ()
    required_workflow: str = ".github/workflows/ci.yml"
    max_pending: int = Field(default=10, ge=1, le=50)
    max_disk_bytes: int = Field(default=10_000_000_000, ge=100_000_000)

    @property
    def bundles(self) -> Path:
        return self.bundle_root or self.root / "bundles"

    @model_validator(mode="after")
    def paths(self) -> ControllerConfig:
        paths = (
            self.root,
            self.policy,
            self.socket,
            self.token_file,
            self.host_socket,
            self.host_token_file,
            self.workspaces,
            self.builder_python,
            self.uv,
            self.dependency_wheels,
            *self.native_files.values(),
            *self.runtime_reads,
        )
        credentials = tuple(
            path
            for path in (
                self.github_key_file,
                self.github_cli,
                self.github_cli_config,
                self.node,
                self.bundle_root,
            )
            if path is not None
        )
        if self.github_auth == "github_app" and not (
            self.github_app_id and self.github_installation_id and self.github_key_file
        ):
            raise ValueError("GitHub App credentials are incomplete")
        if self.github_auth == "gh_cli" and not (self.github_cli and self.github_cli_config):
            raise ValueError(
                "Configure the existing authenticated GitHub CLI and its configuration directory"
            )
        if any(not path.is_absolute() for path in (*paths, *credentials)):
            raise ValueError("Installation paths must be absolute")
        if self.root.is_relative_to(self.workspaces) or self.workspaces.is_relative_to(self.root):
            raise ValueError("Controller and worker directories must be disjoint")
        if self.bundle_root and any(
            self.bundle_root.is_relative_to(path) or path.is_relative_to(self.bundle_root)
            for path in (self.root, self.workspaces)
        ):
            raise ValueError(
                "Shared bundles must be separate from private controller and job state"
            )
        if any(
            self.root.is_relative_to(path)
            or path.is_relative_to(self.root)
            or self.workspaces.is_relative_to(path)
            for path in self.runtime_reads
        ):
            raise ValueError(
                "Runtime read grants must not include controller or workspace authority"
            )
        if len({check.name for check in self.checks}) != len(self.checks):
            raise ValueError("Check names must be unique")
        if any(Path(name).is_absolute() or ".." in Path(name).parts for name in self.native_files):
            raise ValueError("Native bundle destinations must be relative")
        if self.vm and any(
            self.workspaces.is_relative_to(path) or path.is_relative_to(self.workspaces)
            for path in (self.vm.root, self.vm.tart_home)
        ):
            raise ValueError("VM state must be disjoint from writable coding workspaces")
        return self


def read_protected(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 65536:
            raise Denied("Configuration must be a protected bounded regular file")
        with os.fdopen(descriptor, closefd=False) as stream:
            return stream.read(65536)
    finally:
        os.close(descriptor)
