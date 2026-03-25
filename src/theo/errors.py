"""Theo exception hierarchy."""


class TheoError(Exception):
    """Base for all Theo errors."""


class DatabaseNotConnectedError(TheoError):
    """Raised when the connection pool is accessed before connect()."""


