"""Privacy filter pipeline: evaluate trust/content before storage."""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import TYPE_CHECKING, Literal

from opentelemetry import trace

from theo.config import get_settings

if TYPE_CHECKING:
    from theo.memory._types import EpisodeChannel, SensitivityLevel, TrustTier

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

type ContentCategory = Literal[
    "general", "financial", "medical", "identity", "location", "relationship"
]

_SENSITIVITY_ORDER: dict[str, int] = {"normal": 0, "sensitive": 1, "private": 2}


@dataclasses.dataclass(frozen=True, slots=True)
class PrivacyDecision:
    """Result of evaluating a storage request through the privacy pipeline."""

    allowed: bool
    sensitivity: SensitivityLevel
    reason: str


# ---------------------------------------------------------------------------
# Stage 1 — Source trust check
# ---------------------------------------------------------------------------

# Maps trust tier to (allowed, max_sensitivity).
# max_sensitivity is the highest level that tier may store.
_TRUST_RULES: dict[str, tuple[bool, str]] = {
    "owner": (True, "private"),
    "owner_confirmed": (True, "private"),
    "verified": (True, "sensitive"),
    "inferred": (True, "sensitive"),
    "external": (True, "normal"),
    "untrusted": (True, "normal"),
}


def _check_trust(trust: TrustTier) -> tuple[bool, SensitivityLevel]:
    """Return (allowed, max_sensitivity) for the given trust tier."""
    allowed, max_sens = _TRUST_RULES[trust]
    return allowed, max_sens  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Stage 2 — Content classification (heuristic)
# ---------------------------------------------------------------------------

_CATEGORY_PATTERNS: dict[str, re.Pattern[str]] = {
    "financial": re.compile(
        r"\b(?:account\s*(?:number|#|num)|routing\s*(?:number|#|num)|"
        r"ssn|social\s*security|bank\s*(?:account|statement)|"
        r"credit\s*card|debit\s*card|iban|swift\s*code|"
        r"tax\s*(?:id|return|number))\b",
        re.IGNORECASE,
    ),
    "medical": re.compile(
        r"\b(?:diagnosis|diagnosed|prescription|medication|"
        r"health\s*(?:condition|record|insurance)|"
        r"blood\s*(?:type|pressure|test)|"
        r"medical\s*(?:history|record)|"
        r"symptom|therapy|treatment\s*plan)\b",
        re.IGNORECASE,
    ),
    "identity": re.compile(
        r"\b(?:passport\s*(?:number|#|num)?|"
        r"driver'?s?\s*licen[sc]e|"
        r"ssn|social\s*security\s*(?:number|#)?|"
        r"national\s*id|birth\s*certificate|"
        r"biometric)\b",
        re.IGNORECASE,
    ),
    "location": re.compile(
        r"\b(?:home\s*address|street\s*address|"
        r"gps\s*coordinates?|"
        r"latitude|longitude|"
        r"zip\s*code|postal\s*code|"
        r"current\s*location)\b",
        re.IGNORECASE,
    ),
    "relationship": re.compile(
        r"\b(?:affair|divorce|custody|"
        r"intimate\s*(?:partner|relationship)|"
        r"domestic\s*(?:violence|abuse)|"
        r"sexual\s*(?:orientation|identity)|"
        r"estranged|restraining\s*order)\b",
        re.IGNORECASE,
    ),
}


def _classify_content(body: str) -> ContentCategory:
    """Classify body text into a content category using keyword heuristics."""
    for category, pattern in _CATEGORY_PATTERNS.items():
        if pattern.search(body):
            return category  # type: ignore[return-value]
    return "general"  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Stage 3 — Sensitivity assignment
# ---------------------------------------------------------------------------

# Categories that must be at least "sensitive" regardless of trust.
_SENSITIVE_CATEGORIES: frozenset[str] = frozenset({"financial", "medical", "identity"})


def _assign_sensitivity(
    trust: TrustTier,
    category: ContentCategory,
) -> tuple[bool, SensitivityLevel]:
    """Map (trust, category) to (allowed, recommended_sensitivity).

    Returns ``(False, ...)`` when the combination must be rejected.
    """
    _, max_sens = _check_trust(trust)
    max_ord = _SENSITIVITY_ORDER[max_sens]

    # Determine minimum sensitivity for this content category.
    if category in _SENSITIVE_CATEGORIES:
        min_sens: SensitivityLevel = "sensitive"
    else:
        min_sens = "normal"

    min_ord = _SENSITIVITY_ORDER[min_sens]

    # If the minimum required sensitivity exceeds what the trust tier allows,
    # reject the operation. Example: untrusted source + financial content.
    if min_ord > max_ord:
        return False, min_sens

    return True, min_sens


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate(
    body: str,
    *,
    trust: TrustTier,
    sensitivity: SensitivityLevel = "normal",
    channel: EpisodeChannel | None = None,
) -> PrivacyDecision:
    """Run the three-stage privacy pipeline.

    Returns a :class:`PrivacyDecision` indicating whether the storage is
    allowed and the final sensitivity level (which may be escalated but
    never downgraded from *sensitivity*).
    """
    with tracer.start_as_current_span("privacy.evaluate") as span:
        settings = get_settings()

        if not settings.privacy_filter_enabled:
            span.set_attributes(
                {
                    "privacy.trust": trust,
                    "privacy.category": "general",
                    "privacy.decision": "allowed",
                    "privacy.sensitivity": sensitivity,
                }
            )
            return PrivacyDecision(
                allowed=True,
                sensitivity=sensitivity,
                reason="filter disabled",
            )

        # Stage 1: trust check.
        _, max_sens = _check_trust(trust)

        # Stage 2: content classification.
        category = _classify_content(body)

        # Stage 3: sensitivity assignment.
        allowed, recommended_sens = _assign_sensitivity(trust, category)

        # Escalate sensitivity: never downgrade from what the caller passed.
        final_ord = max(
            _SENSITIVITY_ORDER[sensitivity],
            _SENSITIVITY_ORDER[recommended_sens],
        )
        # Also cap at what the trust tier allows.
        capped_ord = min(final_ord, _SENSITIVITY_ORDER[max_sens])
        final_sens: SensitivityLevel = _ord_to_sensitivity(capped_ord)

        if not allowed:
            reason = (
                f"rejected: {trust} source cannot store "
                f"{category} content (requires {recommended_sens})"
            )
            log.warning(
                "privacy filter rejected storage",
                extra={
                    "trust": trust,
                    "category": category,
                    "sensitivity": sensitivity,
                },
            )
        else:
            reason = (
                f"allowed: trust={trust}, category={category}, "
                f"sensitivity {sensitivity}->{final_sens}"
            )

        decision = "rejected" if not allowed else "allowed"

        span.set_attributes(
            {
                "privacy.trust": trust,
                "privacy.category": category,
                "privacy.decision": decision,
                "privacy.sensitivity": final_sens,
            }
        )

        if channel is not None:
            span.set_attribute("privacy.channel", channel)

        return PrivacyDecision(
            allowed=allowed,
            sensitivity=final_sens,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORD_TO_SENSITIVITY: dict[int, SensitivityLevel] = {
    v: k
    for k, v in _SENSITIVITY_ORDER.items()  # type: ignore[misc]
}


def _ord_to_sensitivity(ordinal: int) -> SensitivityLevel:
    return _ORD_TO_SENSITIVITY[ordinal]


def escalate_sensitivity(
    passed: SensitivityLevel,
    recommended: SensitivityLevel,
) -> SensitivityLevel:
    """Return the higher of two sensitivity levels (never downgrade)."""
    return _ord_to_sensitivity(max(_SENSITIVITY_ORDER[passed], _SENSITIVITY_ORDER[recommended]))
