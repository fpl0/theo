"""Application configuration loaded from environment / .env."""

from functools import cache
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="THEO_",
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

    # LLM
    anthropic_api_key: SecretStr
    llm_model_reactive: str = "claude-haiku-4-5-20251001"
    llm_model_reflective: str = "claude-sonnet-4-6-20250514"
    llm_model_deliberative: str = "claude-opus-4-6-20250514"
    llm_max_tokens: int = 4096

    # Embeddings
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    embedding_dim: int = 768

    # Context assembly budgets (approximate token counts)
    context_memory_budget: int = 2000
    context_history_budget: int = 4000

    # Telegram gate
    telegram_bot_token: SecretStr | None = None
    telegram_owner_chat_id: int | None = None

    @model_validator(mode="after")
    def _validate_pool_bounds(self) -> Self:
        if self.db_pool_min > self.db_pool_max:
            msg = f"db_pool_min ({self.db_pool_min}) must be <= db_pool_max ({self.db_pool_max})"
            raise ValueError(msg)
        return self


@cache
def get_settings() -> Settings:
    """Build settings from environment. Fails fast if required vars are missing."""
    return Settings()
