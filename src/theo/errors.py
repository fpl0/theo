"""Theo exception hierarchy."""


class TheoError(Exception):
    """Base for all Theo errors."""


class DatabaseNotConnectedError(TheoError):
    """Raised when the connection pool is accessed before connect()."""


class BusNotRunningError(TheoError):
    """Raised when publishing to a bus that has not been started."""


class APIUnavailableError(TheoError):
    """Raised when the Anthropic API is unreachable or returns a server error."""
