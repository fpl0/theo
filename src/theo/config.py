"""Application configuration loaded from environment / .env."""

from functools import cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="THEO_",
        env_file=(".env", ".env.local"),
        extra="ignore",
    )

    # PostgreSQL
    database_url: SecretStr
    db_pool_min: int = 2
    db_pool_max: int = 5

    # Observability
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    otel_enabled: bool = True
    otel_exporter: Literal["console", "otlp"] = "console"

    # Embeddings
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    embedding_dim: int = 768

    @model_validator(mode="after")
    def _validate_pool_bounds(self) -> Settings:
        if self.db_pool_min > self.db_pool_max:
            msg = f"db_pool_min ({self.db_pool_min}) must be <= db_pool_max ({self.db_pool_max})"
            raise ValueError(msg)
        return self


@cache
def get_settings() -> Settings:
    """Build settings from environment. Fails fast if required vars are missing."""
    return Settings()
