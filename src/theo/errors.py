"""Theo exception hierarchy."""


class TheoError(Exception):
    """Base for all Theo errors."""


class DatabaseNotConnectedError(TheoError):
    """Raised when the connection pool is accessed before connect()."""


class BusNotRunningError(TheoError):
    """Raised when publishing to a bus that has not been started."""


class APIUnavailableError(TheoError):
    """Raised when the Anthropic API is unreachable or returns a server error."""


class ConversationNotRunningError(TheoError):
    """Raised when a message is received by a stopped conversation engine."""


class CircuitOpenError(TheoError):
    """Raised when the circuit breaker is open and rejecting calls."""


class GateConfigError(TheoError):
    """Raised when a gate is missing required configuration."""


class DimensionNotFoundError(TheoError):
    """Raised when a user model dimension does not exist."""


class PrivacyViolationError(TheoError):
    """Raised when the privacy filter rejects a storage operation."""


class SelfModelDomainNotFoundError(TheoError):
    """Raised when a self-model domain does not exist."""


class TranscriptionError(TheoError):
    """Raised when audio transcription fails."""
