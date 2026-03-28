"""Proposal approval gateway — inline keyboard UI for propose/consult actions.

Presents proposals via Telegram inline keyboards and handles owner responses
(approve, modify, reject) plus auto-expiry on timeout. Manages concurrent
proposals with a configurable cap.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from opentelemetry import metrics, trace

if TYPE_CHECKING:
    from uuid import UUID

from theo.bus import bus
from theo.bus.events import ProposalCreated, ProposalExpired, ProposalResponse
from theo.config import get_settings

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher
    from aiogram.types import Message

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

_proposals_sent = _meter.create_counter(
    "theo.proposals.sent",
    description="Total proposals sent to owner",
)
_proposals_approved = _meter.create_counter(
    "theo.proposals.approved",
    description="Total proposals approved by owner",
)
_proposals_rejected = _meter.create_counter(
    "theo.proposals.rejected",
    description="Total proposals rejected by owner",
)
_proposals_modified = _meter.create_counter(
    "theo.proposals.modified",
    description="Total proposals modified by owner",
)
_proposals_expired = _meter.create_counter(
    "theo.proposals.expired",
    description="Total proposals expired without response",
)

# Maximum proposal text length (Telegram message limit minus headroom).
_MAX_PROPOSAL_LENGTH = 1024


def _short_id(proposal_id: UUID) -> str:
    """First 8 hex chars of the proposal UUID."""
    return proposal_id.hex[:8]


def _build_keyboard(short: str) -> InlineKeyboardMarkup:
    """Build the approve/modify/reject inline keyboard for a proposal."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Approve",
                    callback_data=f"proposal:{short}:approve",
                ),
                InlineKeyboardButton(
                    text="Modify",
                    callback_data=f"proposal:{short}:modify",
                ),
                InlineKeyboardButton(
                    text="Reject",
                    callback_data=f"proposal:{short}:reject",
                ),
            ],
        ],
    )


class ProposalGateway:
    """Manages proposal presentation, callbacks, and timeouts via Telegram."""

    def __init__(self, bot: Bot, owner_chat_id: int, dp: Dispatcher) -> None:
        self._bot = bot
        self._owner_chat_id = owner_chat_id

        # State: short-id → data.
        self._pending: dict[str, ProposalCreated] = {}
        self._messages: dict[str, int] = {}  # short-id → telegram message_id
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._awaiting_modification: dict[int, str] = {}  # msg_id → short-id

        # Register callback handler.
        dp.callback_query.register(
            self._on_callback,
            lambda cq: (cq.data or "").startswith("proposal:"),
        )

    # -- lifecycle --------------------------------------------------------

    def subscribe(self) -> None:
        """Subscribe to bus events for proposal lifecycle."""
        bus.subscribe(ProposalCreated, self._on_proposal_created)

    async def shutdown(self) -> None:
        """Cancel all pending timers."""
        for task in self._timers.values():
            task.cancel()
        self._timers.clear()

    # -- bus handler: present proposal ------------------------------------

    async def _on_proposal_created(self, event: ProposalCreated) -> None:
        """Send a proposal to the owner via Telegram inline keyboard."""
        with tracer.start_as_current_span(
            "proposal.send",
            attributes={
                "proposal.id": str(event.proposal_id),
                "proposal.action_type": event.action_type,
            },
        ):
            cfg = get_settings()
            short = _short_id(event.proposal_id)

            # Enforce concurrent cap.
            if len(self._pending) >= cfg.max_pending_proposals:
                log.warning(
                    "proposal cap reached, notifying owner",
                    extra={
                        "pending_count": len(self._pending),
                        "max": cfg.max_pending_proposals,
                    },
                )
                try:
                    await self._bot.send_message(
                        chat_id=self._owner_chat_id,
                        text=(
                            f"Cannot present new proposal — {len(self._pending)} pending "
                            "(max {cfg.max_pending_proposals}). "
                            "Please respond to existing proposals first."
                        ),
                        parse_mode=None,
                    )
                except TelegramAPIError:
                    log.exception("failed to send cap warning")
                return

            # Build proposal text.
            action_label = event.action_type.upper().replace("_", " ")
            text = f"[{action_label}] Proposal\n\nWHAT: {event.summary}\n"
            if event.detail:
                text += f"WHY: {event.detail}\n"
            text += "\nTap a button below, or reply to this message to modify."

            if len(text) > _MAX_PROPOSAL_LENGTH:
                text = text[: _MAX_PROPOSAL_LENGTH - 3] + "..."

            keyboard = _build_keyboard(short)

            try:
                sent = await self._bot.send_message(
                    chat_id=self._owner_chat_id,
                    text=text,
                    parse_mode=None,
                    reply_markup=keyboard,
                )
            except TelegramAPIError:
                log.exception(
                    "failed to send proposal",
                    extra={"proposal_id": str(event.proposal_id)},
                )
                return

            # Track state.
            self._pending[short] = event
            self._messages[short] = sent.message_id

            # Start timeout timer.
            self._timers[short] = asyncio.create_task(
                self._expire_after(short, event.timeout_s),
                name=f"proposal-timeout-{short}",
            )

            _proposals_sent.add(1, {"action_type": event.action_type})
            log.info(
                "proposal sent",
                extra={
                    "proposal_id": str(event.proposal_id),
                    "action_type": event.action_type,
                    "timeout_s": event.timeout_s,
                    "telegram_message_id": sent.message_id,
                },
            )

    # -- callback handler: button taps ------------------------------------

    async def _on_callback(self, callback: CallbackQuery) -> None:
        """Handle approve/modify/reject button taps."""
        data = callback.data or ""
        parts = data.split(":")
        if len(parts) != 3:  # noqa: PLR2004
            return

        _, short, action = parts

        with tracer.start_as_current_span(
            "proposal.callback",
            attributes={"proposal.short_id": short, "proposal.action": action},
        ):
            event = self._pending.get(short)
            if event is None:
                await callback.answer(text="This proposal has expired or was already handled.")
                return

            if action == "approve":
                await self._handle_approve(short, event, callback)
            elif action == "reject":
                await self._handle_reject(short, event, callback)
            elif action == "modify":
                await self._handle_modify_tap(short, callback)
            else:
                await callback.answer(text="Unknown action.")

    async def _handle_approve(
        self,
        short: str,
        event: ProposalCreated,
        callback: CallbackQuery,
    ) -> None:
        """Process an approval response."""
        await callback.answer(text="Approved!")
        self._cleanup(short)

        # Edit message to show approval.
        msg_id = self._messages.pop(short, None)
        if msg_id is not None:
            await self._edit_proposal_status(msg_id, "(Approved)")

        _proposals_approved.add(1, {"action_type": event.action_type})
        log.info("proposal approved", extra={"proposal_id": str(event.proposal_id)})

        await bus.publish(
            ProposalResponse(
                proposal_id=event.proposal_id,
                action="approve",
                session_id=event.session_id,
                channel=event.channel,
            ),
        )

    async def _handle_reject(
        self,
        short: str,
        event: ProposalCreated,
        callback: CallbackQuery,
    ) -> None:
        """Process a rejection response."""
        await callback.answer(text="Rejected.")
        self._cleanup(short)

        msg_id = self._messages.pop(short, None)
        if msg_id is not None:
            await self._edit_proposal_status(msg_id, "(Rejected)")

        _proposals_rejected.add(1, {"action_type": event.action_type})
        log.info("proposal rejected", extra={"proposal_id": str(event.proposal_id)})

        await bus.publish(
            ProposalResponse(
                proposal_id=event.proposal_id,
                action="reject",
                session_id=event.session_id,
                channel=event.channel,
            ),
        )

    async def _handle_modify_tap(self, short: str, callback: CallbackQuery) -> None:
        """Prompt the owner to reply with a modification."""
        await callback.answer(text="Reply to this message with your modification")

        msg_id = self._messages.get(short)
        if msg_id is not None:
            self._awaiting_modification[msg_id] = short
            await self._edit_proposal_status(
                msg_id,
                "(Awaiting modification \u2014 reply to this message)",
                keep_keyboard=True,
            )

    # -- reply handler: modification via reply ----------------------------

    async def try_handle_reply(self, message: Message) -> bool:
        """Check if a message is a reply to a proposal awaiting modification.

        Returns True if handled (caller should not process as normal message).
        """
        reply = message.reply_to_message
        if reply is None:
            return False

        short = self._awaiting_modification.pop(reply.message_id, None)
        if short is None:
            return False

        event = self._pending.get(short)
        if event is None:
            return False

        with tracer.start_as_current_span(
            "proposal.modify",
            attributes={"proposal.short_id": short},
        ):
            modification = message.text or ""
            self._cleanup(short)

            msg_id = self._messages.pop(short, None)
            if msg_id is not None:
                await self._edit_proposal_status(msg_id, "(Modified)")

            _proposals_modified.add(1, {"action_type": event.action_type})
            log.info(
                "proposal modified",
                extra={
                    "proposal_id": str(event.proposal_id),
                    "modification_length": len(modification),
                },
            )

            await bus.publish(
                ProposalResponse(
                    proposal_id=event.proposal_id,
                    action="modify",
                    modification=modification,
                    session_id=event.session_id,
                    channel=event.channel,
                ),
            )
            return True

    # -- timeout ----------------------------------------------------------

    async def _expire_after(self, short: str, timeout_s: int) -> None:
        """Wait *timeout_s* then expire the proposal."""
        await asyncio.sleep(timeout_s)

        with tracer.start_as_current_span(
            "proposal.expire",
            attributes={"proposal.short_id": short},
        ):
            event = self._pending.pop(short, None)
            if event is None:
                return  # Already handled.

            self._timers.pop(short, None)

            msg_id = self._messages.pop(short, None)
            if msg_id is not None:
                await self._edit_proposal_status(msg_id, "(Expired)")
                self._awaiting_modification.pop(msg_id, None)

            _proposals_expired.add(1, {"action_type": event.action_type})
            log.info(
                "proposal expired",
                extra={"proposal_id": str(event.proposal_id)},
            )

            await bus.publish(
                ProposalExpired(
                    proposal_id=event.proposal_id,
                    session_id=event.session_id,
                    channel=event.channel,
                ),
            )

    # -- helpers ----------------------------------------------------------

    def _cleanup(self, short: str) -> None:
        """Remove proposal from pending state and cancel its timer."""
        self._pending.pop(short, None)
        timer = self._timers.pop(short, None)
        if timer is not None:
            timer.cancel()
        # Clean up awaiting_modification for this proposal's message.
        msg_id = self._messages.get(short)
        if msg_id is not None:
            self._awaiting_modification.pop(msg_id, None)

    async def _edit_proposal_status(
        self,
        message_id: int,
        status_suffix: str,
        *,
        keep_keyboard: bool = False,
    ) -> None:
        """Append a status suffix to the proposal message and optionally remove keyboard."""
        try:
            # Fetch doesn't work in aiogram edit — we need to rebuild.
            # Instead, just edit with the suffix appended as a new line.
            # We store original text? No — we just edit reply_markup.
            if keep_keyboard:
                # Only answer_callback_query was used, message text already
                # updated by the caller. For modify, we append status to text.
                # Since we can't easily read the current text, we use a simpler
                # approach: edit only the reply markup.
                pass
            else:
                await self._bot.edit_message_reply_markup(
                    chat_id=self._owner_chat_id,
                    message_id=message_id,
                    reply_markup=None,
                )
        except TelegramAPIError:
            log.warning(
                "failed to edit proposal message",
                extra={"message_id": message_id, "status": status_suffix},
            )

    @property
    def pending_count(self) -> int:
        """Number of currently pending proposals."""
        return len(self._pending)
