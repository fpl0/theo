"""Conversation engine — lifecycle, state management, event dispatch."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal

from theo.bus import bus
from theo.bus.events import MessageReceived
from theo.conversation.turn import execute_turn
from theo.errors import ConversationNotRunningError
from theo.llm import SessionContext, Speed
from theo.resilience import retry_queue

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from uuid import UUID

    from theo.bus.events import Channel, TrustTier

type EngineState = Literal["running", "paused", "stopped"]

log = logging.getLogger(__name__)


class ConversationEngine:
    """Processes incoming messages through the LLM pipeline.

    Lifecycle: ``start`` -> ``pause``/``resume`` -> ``stop``.

    Concurrency: one turn at a time per session. Messages arriving while a
    session is busy are queued and processed in order.
    """

    # Maximum number of prior speeds to keep per session for history bias.
    _HISTORY_WINDOW = 6

    def __init__(self) -> None:
        self._state: EngineState = "stopped"
        self._session_locks: dict[UUID, asyncio.Lock] = {}
        self._session_speeds: dict[UUID, list[Speed]] = {}
        self._paused_queue: asyncio.Queue[MessageReceived] = asyncio.Queue()
        self._inflight = 0
        self._drained = asyncio.Event()
        self._drained.set()

    # -- lifecycle --------------------------------------------------------

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

    # -- session speed tracking -------------------------------------------

    def session_context_for(self, session_id: UUID) -> SessionContext:
        """Build a ``SessionContext`` from recorded session speeds."""
        speeds = self._session_speeds.get(session_id, [])
        if not speeds:
            return SessionContext()
        order = {"reactive": 0, "reflective": 1, "deliberative": 2}
        peak: Speed = max(speeds, key=lambda s: order[s])
        return SessionContext(
            peak_speed=peak,
            prior_speeds=tuple(speeds[-self._HISTORY_WINDOW :]),
        )

    def record_speed(self, session_id: UUID, speed: Speed) -> None:
        """Record a speed classification for a session."""
        speeds = self._session_speeds.setdefault(session_id, [])
        speeds.append(speed)
        # Cap history to avoid unbounded growth.
        if len(speeds) > self._HISTORY_WINDOW:
            self._session_speeds[session_id] = speeds[-self._HISTORY_WINDOW :]

    # -- event handler ----------------------------------------------------

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

    # -- core turn dispatch -----------------------------------------------

    async def _run_under_lock(
        self,
        session_id: UUID,
        coro: Coroutine[Any, Any, None],
    ) -> None:
        """Execute *coro* under the per-session lock with inflight tracking."""
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            self._inflight += 1
            self._drained.clear()
            try:
                await coro
            finally:
                self._inflight -= 1
                if self._inflight == 0:
                    self._drained.set()
        # Clean up session lock if no longer in use.
        # Session speeds are NOT cleaned here — they must persist across
        # turns for the ratchet to work. They are bounded by _HISTORY_WINDOW.
        lock = self._session_locks.get(session_id)
        if lock is not None and not lock.locked():
            self._session_locks.pop(session_id, None)

    async def _process_message(self, event: MessageReceived) -> None:
        """Execute a full conversation turn for one message."""
        session_id = event.session_id
        if session_id is None:
            log.warning("dropping message without session_id", extra={"event_id": str(event.id)})
            return
        ctx = self.session_context_for(session_id)
        await self._run_under_lock(
            session_id,
            execute_turn(event, session_id, session_context=ctx, engine=self),
        )

    async def _retry_message(
        self,
        *,
        session_id: UUID,
        channel: Channel | None,
        body: str,
        trust: TrustTier,
    ) -> None:
        """Re-process a previously failed message (called by the retry queue).

        The user message was already persisted as an episode on the original
        attempt, so we skip persistence and go straight to the LLM call.
        """
        event = MessageReceived(
            body=body,
            session_id=session_id,
            channel=channel,
            trust=trust,
        )
        ctx = self.session_context_for(session_id)
        await self._run_under_lock(
            session_id,
            execute_turn(
                event,
                session_id,
                persist_user_message=False,
                session_context=ctx,
                engine=self,
            ),
        )
