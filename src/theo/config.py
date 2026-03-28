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
    session_ratchet_enabled: bool = True

    # Deliberation
    deliberation_max_phases: int = 5
    deliberation_phase_timeout_s: int = 120
    deliberation_budget_tokens: int = 20_000

    # Metacognition
    metacognition_enabled: bool = True
    metacognition_spinning_threshold: float = 0.85
    metacognition_drift_threshold: float = 0.7
    metacognition_min_evidence_for_high_confidence: int = 3

    # Embeddings
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    embedding_dim: int = 768

    whisper_model: str = "mlx-community/whisper-small"

    # Context assembly budgets (approximate token counts).
    # Persona and goals are never truncated — no budget field needed.
    # User model and current task are capped at their budget when they exceed it.
    context_user_model_budget: int = 400
    context_current_task_budget: int = 300
    context_memory_budget: int = 2000
    context_history_budget: int = 4000

    # Retrieval (hybrid search / RRF fusion)
    retrieval_rrf_k: int = 60
    retrieval_candidate_limit: int = 50
    retrieval_graph_seed_count: int = 5
    retrieval_graph_max_depth: int = 2

    # Memory
    contradiction_check_enabled: bool = True

    # Privacy filtering
    privacy_filter_enabled: bool = True

    # Budget controls
    budget_cost_reactive_per_1k: float = 0.25
    budget_cost_reflective_per_1k: float = 3.0
    budget_cost_deliberative_per_1k: float = 15.0
    budget_daily_cap_tokens: int = 2_000_000
    budget_session_cap_tokens: int = 500_000
    budget_warning_threshold: float = 0.8

    # Telegram gate
    telegram_bot_token: SecretStr | None = None
    telegram_owner_chat_id: int | None = None

    # Proposal approval gateway
    proposal_timeout_propose_s: int = 14400  # 4 hours
    proposal_timeout_consult_s: int = 86400  # 24 hours
    max_pending_proposals: int = 5

    @model_validator(mode="after")
    def _validate_pool_bounds(self) -> Self:
        if self.db_pool_min > self.db_pool_max:
            msg = f"db_pool_min ({self.db_pool_min}) must be <= db_pool_max ({self.db_pool_max})"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_context_budgets(self) -> Self:
        for name in (
            "context_user_model_budget",
            "context_current_task_budget",
            "context_memory_budget",
            "context_history_budget",
        ):
            if getattr(self, name) <= 0:
                msg = f"{name} must be > 0"
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_budget_bounds(self) -> Self:
        if self.budget_daily_cap_tokens < 1:
            msg = "budget_daily_cap_tokens must be >= 1"
            raise ValueError(msg)
        if self.budget_session_cap_tokens < 1:
            msg = "budget_session_cap_tokens must be >= 1"
            raise ValueError(msg)
        if not 0 < self.budget_warning_threshold < 1:
            msg = "budget_warning_threshold must be between 0 and 1 (exclusive)"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_retrieval_bounds(self) -> Self:
        if self.retrieval_candidate_limit < 1:
            msg = "retrieval_candidate_limit must be >= 1"
            raise ValueError(msg)
        if self.retrieval_graph_seed_count < 1:
            msg = "retrieval_graph_seed_count must be >= 1"
            raise ValueError(msg)
        if self.retrieval_graph_max_depth < 1:
            msg = "retrieval_graph_max_depth must be >= 1"
            raise ValueError(msg)
        if self.retrieval_graph_seed_count > self.retrieval_candidate_limit:
            msg = "retrieval_graph_seed_count must be <= retrieval_candidate_limit"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_metacognition(self) -> Self:
        if not (0.0 < self.metacognition_spinning_threshold <= 1.0):
            msg = "metacognition_spinning_threshold must be in (0, 1]"
            raise ValueError(msg)
        if not (0.0 < self.metacognition_drift_threshold <= 1.0):
            msg = "metacognition_drift_threshold must be in (0, 1]"
            raise ValueError(msg)
        if self.metacognition_min_evidence_for_high_confidence < 1:
            msg = "metacognition_min_evidence_for_high_confidence must be >= 1"
            raise ValueError(msg)
        return self


@cache
def get_settings() -> Settings:
    """Build settings from environment. Fails fast if required vars are missing."""
    return Settings()
