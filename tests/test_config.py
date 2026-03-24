import pytest
from pydantic import ValidationError

from theo.config import Settings


def test_accepts_database_url() -> None:
    s = Settings(database_url="postgresql://u:p@h:5432/d")
    assert s.database_url.get_secret_value() == "postgresql://u:p@h:5432/d"


def test_rejects_missing_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THEO_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_pool_defaults() -> None:
    s = Settings(database_url="postgresql://u:p@h:5432/d")
    assert s.db_pool_min == 2
    assert s.db_pool_max == 5


def test_pool_override() -> None:
    s = Settings(database_url="postgresql://u:p@h:5432/d", db_pool_min=1, db_pool_max=5)
    assert s.db_pool_min == 1
    assert s.db_pool_max == 5
