"""Tests for theo.memory.privacy — three-stage privacy filter pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import numpy as np
import pytest

from theo.config import Settings
from theo.errors import PrivacyViolationError
from theo.memory.episodes import store_episode
from theo.memory.nodes import store_node
from theo.memory.privacy import (
    PrivacyDecision,
    _classify_content,
    escalate_sensitivity,
    evaluate,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DIM = 768


def _fake_vector() -> np.ndarray:
    vec = np.random.default_rng(42).standard_normal(_DIM).astype(np.float32)
    return vec / np.linalg.norm(vec)


def _settings(**overrides: object) -> Settings:
    defaults = {
        "database_url": "postgresql://x:x@localhost/x",
        "anthropic_api_key": "sk-test",
        "_env_file": None,
    }
    return Settings(**(defaults | overrides))


@pytest.fixture
def enabled_settings() -> Settings:
    return _settings(privacy_filter_enabled=True)


@pytest.fixture
def disabled_settings() -> Settings:
    return _settings(privacy_filter_enabled=False)


# ---------------------------------------------------------------------------
# PrivacyDecision dataclass
# ---------------------------------------------------------------------------


def test_privacy_decision_is_frozen() -> None:
    d = PrivacyDecision(allowed=True, sensitivity="normal", reason="ok")
    with pytest.raises(AttributeError):
        d.allowed = False  # type: ignore[misc]


def test_privacy_decision_has_slots() -> None:
    assert "__slots__" in dir(PrivacyDecision)


# ---------------------------------------------------------------------------
# Stage 2 — Content classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("The weather is nice today", "general"),
        ("My bank account number is 123456", "financial"),
        ("Please send to my routing number", "financial"),
        ("SSN 123-45-6789", "financial"),
        ("My credit card ends in 4242", "financial"),
        ("The diagnosis was positive", "medical"),
        ("New prescription for medication", "medical"),
        ("My health condition has improved", "medical"),
        ("Blood pressure reading is 120/80", "medical"),
        ("My passport number is AB123456", "identity"),
        ("Renewed my driver's license", "identity"),
        ("My social security number", "financial"),
        ("National ID card expired", "identity"),
        ("My home address is 123 Main St", "location"),
        ("GPS coordinates 40.7128, -74.0060", "location"),
        ("The zip code is 10001", "location"),
        ("Going through a divorce", "relationship"),
        ("Intimate partner struggles", "relationship"),
        ("Domestic violence situation", "relationship"),
        ("Sexual orientation discussion", "relationship"),
    ],
)
def test_classify_content(body: str, expected: str) -> None:
    assert _classify_content(body) == expected


# ---------------------------------------------------------------------------
# Stage 1+3 — Trust tier decisions
# ---------------------------------------------------------------------------


class TestOwnerTrust:
    """Owner and owner_confirmed have full access."""

    def test_owner_allows_general(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("hello world", trust="owner")
        assert d.allowed is True
        assert d.sensitivity == "normal"

    def test_owner_allows_financial(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("my bank account is 12345", trust="owner")
        assert d.allowed is True
        assert d.sensitivity == "sensitive"

    def test_owner_allows_private_sensitivity(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("hello", trust="owner", sensitivity="private")
        assert d.allowed is True
        assert d.sensitivity == "private"

    def test_owner_confirmed_same_as_owner(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("my bank account 12345", trust="owner_confirmed")
        assert d.allowed is True
        assert d.sensitivity == "sensitive"


class TestVerifiedTrust:
    """Verified can store normal and sensitive, not private."""

    def test_verified_allows_general(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("hello", trust="verified")
        assert d.allowed is True
        assert d.sensitivity == "normal"

    def test_verified_allows_medical(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("diagnosis confirmed", trust="verified")
        assert d.allowed is True
        assert d.sensitivity == "sensitive"

    def test_verified_caps_at_sensitive(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("hello", trust="verified", sensitivity="private")
        assert d.allowed is True
        assert d.sensitivity == "sensitive"


class TestInferredTrust:
    """Inferred can store normal and sensitive, not private."""

    def test_inferred_allows_general(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("hello", trust="inferred")
        assert d.allowed is True
        assert d.sensitivity == "normal"

    def test_inferred_allows_financial(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("bank account info", trust="inferred")
        assert d.allowed is True
        assert d.sensitivity == "sensitive"


class TestExternalTrust:
    """External can only store normal sensitivity."""

    def test_external_allows_general(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("hello", trust="external")
        assert d.allowed is True
        assert d.sensitivity == "normal"

    def test_external_rejects_financial(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("bank account 12345", trust="external")
        assert d.allowed is False

    def test_external_rejects_medical(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("diagnosis confirmed", trust="external")
        assert d.allowed is False

    def test_external_rejects_identity(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("passport number AB123", trust="external")
        assert d.allowed is False


class TestUntrustedTrust:
    """Untrusted can only store normal sensitivity."""

    def test_untrusted_allows_general(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("hello", trust="untrusted")
        assert d.allowed is True
        assert d.sensitivity == "normal"

    def test_untrusted_rejects_financial(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("bank account 12345", trust="untrusted")
        assert d.allowed is False

    def test_untrusted_allows_relationship(self, enabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate("going through a divorce", trust="untrusted")
        assert d.allowed is True  # relationship is not in _SENSITIVE_CATEGORIES
        assert d.sensitivity == "normal"


# ---------------------------------------------------------------------------
# Sensitivity escalation
# ---------------------------------------------------------------------------


class TestSensitivityEscalation:
    def test_escalate_normal_to_sensitive(self) -> None:
        assert escalate_sensitivity("normal", "sensitive") == "sensitive"

    def test_escalate_sensitive_to_private(self) -> None:
        assert escalate_sensitivity("sensitive", "private") == "private"

    def test_never_downgrade(self) -> None:
        assert escalate_sensitivity("sensitive", "normal") == "sensitive"
        assert escalate_sensitivity("private", "normal") == "private"
        assert escalate_sensitivity("private", "sensitive") == "private"

    def test_same_level(self) -> None:
        assert escalate_sensitivity("normal", "normal") == "normal"
        assert escalate_sensitivity("sensitive", "sensitive") == "sensitive"

    def test_medical_content_escalates_from_normal(
        self,
        enabled_settings: Settings,
    ) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate(
                "diagnosis confirmed",
                trust="owner",
                sensitivity="normal",
            )
        assert d.sensitivity == "sensitive"

    def test_passed_private_not_downgraded(
        self,
        enabled_settings: Settings,
    ) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
            d = evaluate(
                "hello world",
                trust="owner",
                sensitivity="private",
            )
        assert d.sensitivity == "private"


# ---------------------------------------------------------------------------
# Filter disabled mode
# ---------------------------------------------------------------------------


class TestDisabledMode:
    def test_disabled_allows_everything(self, disabled_settings: Settings) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=disabled_settings):
            d = evaluate("bank account 12345", trust="untrusted")
        assert d.allowed is True
        assert d.reason == "filter disabled"

    def test_disabled_preserves_passed_sensitivity(
        self,
        disabled_settings: Settings,
    ) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=disabled_settings):
            d = evaluate("hello", trust="external", sensitivity="private")
        assert d.sensitivity == "private"

    def test_disabled_returns_normal_by_default(
        self,
        disabled_settings: Settings,
    ) -> None:
        with patch("theo.memory.privacy.get_settings", return_value=disabled_settings):
            d = evaluate("hello", trust="untrusted")
        assert d.sensitivity == "normal"


# ---------------------------------------------------------------------------
# Channel passthrough
# ---------------------------------------------------------------------------


def test_channel_recorded_in_decision(enabled_settings: Settings) -> None:
    with patch("theo.memory.privacy.get_settings", return_value=enabled_settings):
        d = evaluate("hello", trust="owner", channel="message")
    assert d.allowed is True


# ---------------------------------------------------------------------------
# Integration: store_node raises PrivacyViolationError
# ---------------------------------------------------------------------------


async def test_store_node_raises_on_rejection() -> None:
    settings = _settings(privacy_filter_enabled=True)
    mock_pool = AsyncMock()
    mock_embedder = AsyncMock()
    mock_embedder.embed_one.return_value = _fake_vector()

    with (
        patch("theo.memory.nodes.db", pool=mock_pool),
        patch("theo.memory.nodes.embedder", mock_embedder),
        patch("theo.memory.privacy.get_settings", return_value=settings),
        pytest.raises(PrivacyViolationError, match="rejected"),
    ):
        await store_node(
            kind="fact",
            body="bank account number 12345",
            trust="untrusted",
        )

    mock_pool.fetchval.assert_not_awaited()


async def test_store_node_escalates_sensitivity() -> None:
    settings = _settings(privacy_filter_enabled=True)
    mock_pool = AsyncMock()
    mock_pool.fetchval.return_value = 42
    mock_embedder = AsyncMock()
    mock_embedder.embed_one.return_value = _fake_vector()

    with (
        patch("theo.memory.nodes.db", pool=mock_pool),
        patch("theo.memory.nodes.embedder", mock_embedder),
        patch("theo.memory.privacy.get_settings", return_value=settings),
    ):
        result = await store_node(
            kind="fact",
            body="diagnosis confirmed today",
            trust="owner",
            sensitivity="normal",
        )

    assert result == 42
    # The sensitivity arg passed to DB should be "sensitive" (escalated).
    args = mock_pool.fetchval.call_args.args
    assert args[7] == "sensitive"


async def test_store_node_allows_with_filter_disabled() -> None:
    settings = _settings(privacy_filter_enabled=False)
    mock_pool = AsyncMock()
    mock_pool.fetchval.return_value = 1
    mock_embedder = AsyncMock()
    mock_embedder.embed_one.return_value = _fake_vector()

    with (
        patch("theo.memory.nodes.db", pool=mock_pool),
        patch("theo.memory.nodes.embedder", mock_embedder),
        patch("theo.memory.privacy.get_settings", return_value=settings),
    ):
        result = await store_node(
            kind="fact",
            body="bank account 12345",
            trust="untrusted",
        )

    assert result == 1
    mock_pool.fetchval.assert_awaited_once()


# ---------------------------------------------------------------------------
# Integration: store_episode raises PrivacyViolationError
# ---------------------------------------------------------------------------


async def test_store_episode_raises_on_rejection() -> None:
    settings = _settings(privacy_filter_enabled=True)
    mock_pool = AsyncMock()
    mock_embedder = AsyncMock()
    mock_embedder.embed_one.return_value = _fake_vector()

    with (
        patch("theo.memory.episodes.db", pool=mock_pool),
        patch("theo.memory.episodes.embedder", mock_embedder),
        patch("theo.memory.privacy.get_settings", return_value=settings),
        pytest.raises(PrivacyViolationError, match="rejected"),
    ):
        await store_episode(
            session_id=uuid4(),
            role="user",
            body="bank account number 12345",
            trust="external",
            channel="message",
        )

    mock_pool.fetchval.assert_not_awaited()


async def test_store_episode_escalates_sensitivity() -> None:
    settings = _settings(privacy_filter_enabled=True)
    mock_pool = AsyncMock()
    mock_pool.fetchval.return_value = 99
    mock_embedder = AsyncMock()
    mock_embedder.embed_one.return_value = _fake_vector()

    with (
        patch("theo.memory.episodes.db", pool=mock_pool),
        patch("theo.memory.episodes.embedder", mock_embedder),
        patch("theo.memory.privacy.get_settings", return_value=settings),
    ):
        result = await store_episode(
            session_id=uuid4(),
            role="user",
            body="diagnosis confirmed",
            trust="owner",
            sensitivity="normal",
            channel="message",
        )

    assert result == 99
    args = mock_pool.fetchval.call_args.args
    assert args[8] == "sensitive"  # sensitivity param (position 8 in INSERT)


async def test_store_node_caps_sensitivity_for_verified_trust() -> None:
    settings = _settings(privacy_filter_enabled=True)
    mock_pool = AsyncMock()
    mock_pool.fetchval.return_value = 42
    mock_embedder = AsyncMock()
    mock_embedder.embed_one.return_value = _fake_vector()

    with (
        patch("theo.memory.nodes.db", pool=mock_pool),
        patch("theo.memory.nodes.embedder", mock_embedder),
        patch("theo.memory.privacy.get_settings", return_value=settings),
    ):
        result = await store_node(
            kind="fact",
            body="hello world",
            trust="verified",
            sensitivity="private",
        )

    assert result == 42
    args = mock_pool.fetchval.call_args.args
    assert args[7] == "sensitive"  # capped from private to sensitive
