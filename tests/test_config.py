import pytest
from pydantic import ValidationError

from theo.config import Settings

_REQUIRED = {
    "database_url": "postgresql://u:p@h:5432/d",
    "anthropic_api_key": "sk-test",
}


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_REQUIRED, **overrides}, _env_file=None)  # type: ignore[arg-type]


def test_accepts_database_url() -> None:
    s = _settings()
    assert s.database_url.get_secret_value() == "postgresql://u:p@h:5432/d"


def test_rejects_missing_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THEO_DATABASE_URL", raising=False)
    monkeypatch.delenv("THEO_ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_pool_defaults() -> None:
    s = _settings()
    assert s.db_pool_min == 2
    assert s.db_pool_max == 5


def test_pool_override() -> None:
    s = _settings(db_pool_min=1, db_pool_max=5)
    assert s.db_pool_min == 1
    assert s.db_pool_max == 5


def test_pool_min_exceeds_max_rejected() -> None:
    with pytest.raises(ValidationError, match="db_pool_min"):
        _settings(db_pool_min=10, db_pool_max=2)


def test_context_budget_zero_rejected() -> None:
    with pytest.raises(ValidationError, match="context_memory_budget"):
        _settings(context_memory_budget=0)


def test_retrieval_seed_exceeds_candidate_rejected() -> None:
    with pytest.raises(ValidationError, match="retrieval_graph_seed_count"):
        _settings(retrieval_graph_seed_count=100, retrieval_candidate_limit=10)


def test_metacognition_spinning_threshold_above_one_rejected() -> None:
    with pytest.raises(ValidationError, match="metacognition_spinning_threshold"):
        _settings(metacognition_spinning_threshold=1.5)


def test_metacognition_drift_threshold_zero_rejected() -> None:
    with pytest.raises(ValidationError, match="metacognition_drift_threshold"):
        _settings(metacognition_drift_threshold=0.0)


def test_metacognition_min_evidence_zero_rejected() -> None:
    with pytest.raises(ValidationError, match="metacognition_min_evidence"):
        _settings(metacognition_min_evidence_for_high_confidence=0)
