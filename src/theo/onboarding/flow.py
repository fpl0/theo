"""Onboarding state machine: start, advance, complete, resume."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import UTC, datetime
from typing import Any

from opentelemetry import metrics, trace

from theo.memory import core
from theo.onboarding.prompts import PHASES

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)
_phase_advances = _meter.create_counter(
    "theo.onboarding.phase_advances",
    description="Total onboarding phase advances",
)

_CONTEXT_KEY = "onboarding"
_COMPLETED_KEY = "onboarding_completed"
_flow_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class OnboardingState:
    """Snapshot of the onboarding progress, stored in core_memory.context."""

    phase: str
    phase_index: int
    started_at: str
    completed_phases: tuple[str, ...]


def _state_to_dict(state: OnboardingState) -> dict[str, Any]:
    return {
        "phase": state.phase,
        "phase_index": state.phase_index,
        "started_at": state.started_at,
        "completed_phases": list(state.completed_phases),
    }


def dict_to_state(data: dict[str, Any]) -> OnboardingState:
    phase_index = data["phase_index"]
    phase = data["phase"]

    if not (0 <= phase_index < len(PHASES)):
        msg = f"phase_index {phase_index} out of range [0, {len(PHASES)})"
        raise ValueError(msg)

    if phase != PHASES[phase_index]:
        msg = f"phase {phase!r} does not match PHASES[{phase_index}]={PHASES[phase_index]!r}"
        raise ValueError(msg)

    return OnboardingState(
        phase=phase,
        phase_index=phase_index,
        started_at=data["started_at"],
        completed_phases=tuple(data["completed_phases"]),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_onboarding_state() -> OnboardingState | None:
    """Read the current onboarding state from core_memory.context.

    Returns ``None`` if onboarding has not been started or was completed.
    """
    with tracer.start_as_current_span("get_onboarding_state"):
        doc = await core.read_one("context")
        raw = doc.body.get(_CONTEXT_KEY)
        if raw is None or not isinstance(raw, dict):
            return None
        return dict_to_state(raw)


async def is_onboarding_completed() -> bool:
    """Check whether onboarding was previously completed."""
    with tracer.start_as_current_span("is_onboarding_completed"):
        doc = await core.read_one("context")
        return bool(doc.body.get(_COMPLETED_KEY))


async def start_onboarding() -> OnboardingState:
    """Initialize onboarding at phase 0 (welcome) and persist to core_memory.

    Clears any previous ``onboarding_completed`` flag so a reset works cleanly.
    """
    async with _flow_lock:
        with tracer.start_as_current_span(
            "start_onboarding",
            attributes={"onboarding.phase": PHASES[0]},
        ):
            state = OnboardingState(
                phase=PHASES[0],
                phase_index=0,
                started_at=datetime.now(UTC).isoformat(),
                completed_phases=(),
            )
            doc = await core.read_one("context")
            body = {k: v for k, v in doc.body.items() if k != _COMPLETED_KEY}
            body[_CONTEXT_KEY] = _state_to_dict(state)
            await core.update("context", body=body, reason="onboarding started")
            log.info("onboarding started", extra={"phase": state.phase})
            return state


async def advance_phase(current_state: OnboardingState) -> OnboardingState | None:
    """Move to the next phase. Returns ``None`` if onboarding is now complete."""
    async with _flow_lock:
        with tracer.start_as_current_span(
            "advance_onboarding_phase",
            attributes={"onboarding.from_phase": current_state.phase},
        ) as span:
            next_index = current_state.phase_index + 1
            completed = (*current_state.completed_phases, current_state.phase)

            if next_index >= len(PHASES):
                span.set_attribute("onboarding.completed", value=True)
                await _complete_onboarding_unlocked()
                log.info("onboarding completed (all phases done)")
                return None

            next_phase = PHASES[next_index]
            span.set_attribute("onboarding.to_phase", next_phase)
            span.set_attribute("onboarding.phase_index", next_index)

            new_state = OnboardingState(
                phase=next_phase,
                phase_index=next_index,
                started_at=current_state.started_at,
                completed_phases=completed,
            )
            await _write_state(
                new_state,
                reason=f"advanced from {current_state.phase} to {next_phase}",
            )
            _phase_advances.add(1, {"onboarding.to_phase": next_phase})
            log.info(
                "onboarding phase advanced",
                extra={"from": current_state.phase, "to": next_phase, "index": next_index},
            )
            return new_state


async def complete_onboarding() -> None:
    """Mark onboarding as done by removing the active state and setting a completion flag."""
    async with _flow_lock:
        await _complete_onboarding_unlocked()


async def _complete_onboarding_unlocked() -> None:
    """Inner implementation called with ``_flow_lock`` already held."""
    with tracer.start_as_current_span("complete_onboarding"):
        doc = await core.read_one("context")
        body = {k: v for k, v in doc.body.items() if k != _CONTEXT_KEY}
        body[_COMPLETED_KEY] = True
        await core.update("context", body=body, reason="onboarding completed")
        log.info("onboarding state cleared from context")


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


async def _write_state(state: OnboardingState, *, reason: str) -> None:
    """Merge onboarding state into core_memory.context JSONB."""
    doc = await core.read_one("context")
    body = {**doc.body, _CONTEXT_KEY: _state_to_dict(state)}
    await core.update("context", body=body, reason=reason)
