"""Resilience: circuit breaker, retry queue, health check."""

from opentelemetry import metrics

from theo.resilience.circuit import CircuitBreaker, CircuitState
from theo.resilience.health import HealthStatus, health_check
from theo.resilience.retry import RetryQueue

circuit_breaker = CircuitBreaker()
retry_queue = RetryQueue()

_meter = metrics.get_meter(__name__)


def _observe_circuit_state() -> int:
    return circuit_breaker.state_value


_meter.create_observable_gauge(
    "theo.resilience.circuit_state",
    callbacks=[lambda _options: [metrics.Observation(value=_observe_circuit_state())]],
    description="Circuit breaker state: 0=closed, 1=open, 2=half-open",
)

__all__ = [
    "CircuitBreaker",
    "CircuitState",
    "HealthStatus",
    "RetryQueue",
    "circuit_breaker",
    "health_check",
    "retry_queue",
]
