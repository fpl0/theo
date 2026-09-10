"""Pinned VM inputs and explicit resource limits for the trusted build driver."""

from pathlib import Path
from typing import Annotated

from pydantic import Field, model_validator

from theo.domain import StrictModel

Checksum = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class VmSettings(StrictModel):
    root: Path
    tart_home: Path
    tart: Path
    tart_sha256: Checksum
    base_name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    base_files: dict[str, Checksum]
    python_archive: Path
    python_sha256: Checksum
    uv: Path
    uv_sha256: Checksum
    agent: Path
    agent_sha256: Checksum
    cpus: int = Field(default=4, ge=4, le=8)
    memory_mib: int = Field(default=4096, ge=4096, le=8192)
    timeout: int = Field(default=3600, ge=300, le=7200)
    max_host_memory_bytes: int = Field(default=8_000_000_000, ge=6_000_000_000)
    max_virtual_disk_bytes: int = Field(default=150_000_000_000, ge=50_000_000_000)
    min_free_disk_bytes: int = Field(default=20_000_000_000, ge=10_000_000_000)
    max_input_bytes: int = Field(default=3_000_000_000, ge=100_000_000, le=5_000_000_000)

    @model_validator(mode="after")
    def paths(self) -> VmSettings:
        if any(
            not path.is_absolute()
            for path in (
                self.root,
                self.tart_home,
                self.tart,
                self.python_archive,
                self.uv,
                self.agent,
            )
        ):
            raise ValueError("VM installation paths must be absolute")
        if set(self.base_files) != {"disk.img", "config.json", "nvram.bin"}:
            raise ValueError("Pin the complete Mac VM base image")
        if self.memory_mib * 1024 * 1024 >= self.max_host_memory_bytes:
            raise ValueError("The host memory limit must allow for virtualization overhead")
        return self
