"""Transport contracts used by outbox dispatch and channel senders.

Sender returns remote receipt metadata. NoEffect represents positive evidence
that a request was rejected before a remote effect occurred.
"""

from collections.abc import Awaitable, Callable

from theo.domain import Json

type Sender = Callable[[str, Json], Awaitable[Json]]


class NoEffect(Exception):
    """The transport has positive evidence the request was rejected before effect."""

    def __init__(self, reason: str, retry_after: float | None = None):
        super().__init__(reason)
        self.retry_after = retry_after
