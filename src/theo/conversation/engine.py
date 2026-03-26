"""Conversation engine — lifecycle, state management, event dispatch."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

from theo.bus import bus
from theo.bus.events import MessageReceived
from theo.conversation.turn import execute_turn
from theo.errors import ConversationNotRunningError
from theo.resilience import retry_queue

if TYPE_CHECKING:
    from uuid import UUID

type EngineState = Literal["running", "paused", "stopped"]

log = logging.getLogger(__name__)


class ConversationEngine:
    """Processes incoming messages through the LLM pipeline.

    Lifecycle: ``start`` → ``pause``/``resume`` → ``stop``.

    Concurrency: one turn at a time per session. Messages arriving while a
    session is busy are queued and processed in order.
    """

    def __init__(self) -> None:
        self._state: EngineState = "stopped"
        # Per-session lock ensures sequential processing within a session.
        self._session_locks: dict[UUID, asyncio.Lock] = {}
        # Internal queue for messages received while paused.
        self._paused_queue: asyncio.Queue[MessageReceived] = asyncio.Queue()
        # Track in-flight turns for clean shutdown drain.
        self._inflight = 0
        self._drained = asyncio.Event()
        self._drained.set()  # starts drained (no work in progress)

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the engine and subscribe to the event bus."""
        if self._state == "running":
            return
        self._state = "running"
        bus.subscribe(MessageReceived, self._on_message)
        retry_queue.start(self._retry_message)
        log.info("conversation engine started")

    def pause(self) -> None:
        """Pause processing — messages are queued but not processed."""
        if self._state != "running":
            return
        self._state = "paused"
        log.info("conversation engine paused")

    async def resume(self) -> None:
        """Resume processing and drain any queued messages."""
        if self._state != "paused":
            return
        self._state = "running"
        log.info("conversation engine resumed")
        await self._drain_paused_queue()

    async def stop(self) -> None:
        """Stop accepting messages and wait for in-flight turns to finish."""
        if self._state == "stopped":
            return
        self._state = "stopped"
        await retry_queue.stop()
        # Wait for any in-flight turns to complete.
        await self._drained.wait()
        log.info("conversation engine stopped")

    def kill(self) -> None:
        """Immediately halt without waiting for in-flight turns."""
        self._state = "stopped"
        self._drained.set()
        log.info("conversation engine killed")

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def inflight(self) -> int:
        """Number of turns currently being processed."""
        return self._inflight

    @property
    def queue_depth(self) -> int:
        """Number of messages queued while paused."""
        return self._paused_queue.qsize()

    # ── event handler ─────────────────────────────────────────────────

    async def _on_message(self, event: MessageReceived) -> None:
        """Bus handler for MessageReceived events."""
        if self._state == "stopped":
            raise ConversationNotRunningError
        if self._state == "paused":
            self._paused_queue.put_nowait(event)
            log.debug("queued message while paused", extra={"event_id": str(event.id)})
            return
        await self._process_message(event)

    async def _drain_paused_queue(self) -> None:
        """Process all messages that arrived while paused."""
        while not self._paused_queue.empty():
            event = self._paused_queue.get_nowait()
            await self._process_message(event)

    # ── core turn dispatch ────────────────────────────────────────────

    async def _process_message(self, event: MessageReceived) -> None:
        """Execute a full conversation turn for one message."""
        session_id = event.session_id
        if session_id is None:
            log.warning("dropping message without session_id", extra={"event_id": str(event.id)})
            return

        lock = self._session_locks.setdefault(session_id, asyncio.Lock())

        async with lock:
            self._inflight += 1
            self._drained.clear()
            try:
                await execute_turn(event, session_id)
            finally:
                self._inflight -= 1
                if self._inflight == 0:
                    self._drained.set()

        # Clean up session lock if no longer in use.
        lock = self._session_locks.get(session_id)
        if lock is not None and not lock.locked():
            self._session_locks.pop(session_id, None)

    async def _retry_message(
        self,
        *,
        session_id: UUID,
        channel: str | None,
        body: str,
        trust: str,
    ) -> None:
        """Re-process a previously failed message (called by the retry queue).

        The user message was already persisted as an episode on the original
        attempt, so we skip persistence and go straight to the LLM call.
        """
        event = MessageReceived(
            body=body,
            session_id=session_id,
            channel=channel,  # type: ignore[arg-type]
            trust=trust,  # type: ignore[arg-type]
        )
        await execute_turn(event, session_id, persist_user_message=False)
