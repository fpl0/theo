"""Tests for the proposal approval gateway."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from theo.bus import bus
from theo.bus.events import ProposalCreated, ProposalExpired, ProposalResponse
from theo.config import Settings
from theo.gates.proposals import ProposalGateway, _build_keyboard, _short_id

_TEST_TOKEN = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
_OWNER_CHAT_ID = 42
_SESSION_ID = UUID("00000000-0000-0000-0000-000000000001")


def _settings(**overrides: object) -> Settings:
    defaults = {
        "database_url": "postgresql://x:x@localhost/x",
        "anthropic_api_key": "sk-test",
        "_env_file": None,
    }
    return Settings(**(defaults | overrides))


def _make_gateway() -> ProposalGateway:
    """Create a ProposalGateway with mocked bot and dispatcher."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    bot.edit_message_reply_markup = AsyncMock()
    dp = MagicMock()
    dp.callback_query = MagicMock()
    dp.callback_query.register = MagicMock()
    return ProposalGateway(bot, _OWNER_CHAT_ID, dp)


def _make_proposal(
    *,
    proposal_id: UUID | None = None,
    action_type: str = "external_action",
    summary: str = "Send weekly summary email",
    detail: str = "You asked me to keep your team updated every Friday",
    timeout_s: int = 14400,
) -> ProposalCreated:
    return ProposalCreated(
        proposal_id=proposal_id or uuid4(),
        action_type=action_type,
        summary=summary,
        detail=detail,
        timeout_s=timeout_s,
        session_id=_SESSION_ID,
        channel="message",
    )


def _make_callback(data: str) -> MagicMock:
    """Create a mock CallbackQuery."""
    cq = MagicMock()
    cq.data = data
    cq.answer = AsyncMock()
    return cq


def _make_reply_message(reply_to_msg_id: int, text: str = "my modification") -> MagicMock:
    """Create a mock Message that is a reply to a specific message."""
    msg = MagicMock()
    msg.text = text
    msg.reply_to_message = MagicMock()
    msg.reply_to_message.message_id = reply_to_msg_id
    return msg


class TestShortId:
    def test_returns_first_8_hex_chars(self) -> None:
        pid = UUID("abcdef12-3456-7890-abcd-ef1234567890")
        assert _short_id(pid) == "abcdef12"

    def test_different_uuids_different_shorts(self) -> None:
        a = _short_id(uuid4())
        b = _short_id(uuid4())
        assert a != b


class TestBuildKeyboard:
    def test_has_three_buttons(self) -> None:
        kb = _build_keyboard("abc12345")
        assert len(kb.inline_keyboard) == 1
        row = kb.inline_keyboard[0]
        assert len(row) == 3

    def test_button_labels(self) -> None:
        kb = _build_keyboard("abc12345")
        labels = [btn.text for btn in kb.inline_keyboard[0]]
        assert labels == ["Approve", "Modify", "Reject"]

    def test_callback_data_format(self) -> None:
        kb = _build_keyboard("abc12345")
        data = [btn.callback_data for btn in kb.inline_keyboard[0]]
        assert data == [
            "proposal:abc12345:approve",
            "proposal:abc12345:modify",
            "proposal:abc12345:reject",
        ]

    def test_callback_data_within_telegram_limit(self) -> None:
        kb = _build_keyboard("12345678")
        max_telegram_callback_data = 64
        for btn in kb.inline_keyboard[0]:
            assert len(btn.callback_data) <= max_telegram_callback_data


class TestProposalSending:
    async def test_sends_proposal_message(self) -> None:
        gw = _make_gateway()
        sent = MagicMock()
        sent.message_id = 200
        gw._bot.send_message = AsyncMock(return_value=sent)

        proposal = _make_proposal()
        settings = _settings()

        with patch("theo.gates.proposals.get_settings", return_value=settings):
            await gw._on_proposal_created(proposal)

        gw._bot.send_message.assert_called_once()
        call_kwargs = gw._bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == _OWNER_CHAT_ID
        assert call_kwargs["parse_mode"] is None
        assert "EXTERNAL ACTION" in call_kwargs["text"]
        assert "Send weekly summary" in call_kwargs["text"]
        assert call_kwargs["reply_markup"] is not None

    async def test_tracks_pending_state(self) -> None:
        gw = _make_gateway()
        sent = MagicMock()
        sent.message_id = 200
        gw._bot.send_message = AsyncMock(return_value=sent)

        proposal = _make_proposal()
        short = _short_id(proposal.proposal_id)
        settings = _settings()

        with patch("theo.gates.proposals.get_settings", return_value=settings):
            await gw._on_proposal_created(proposal)

        assert short in gw._pending
        assert gw._messages[short] == 200
        assert short in gw._timers
        assert gw.pending_count == 1

    async def test_includes_detail_when_present(self) -> None:
        gw = _make_gateway()
        sent = MagicMock()
        sent.message_id = 200
        gw._bot.send_message = AsyncMock(return_value=sent)

        proposal = _make_proposal(detail="Because reasons")
        settings = _settings()

        with patch("theo.gates.proposals.get_settings", return_value=settings):
            await gw._on_proposal_created(proposal)

        text = gw._bot.send_message.call_args.kwargs["text"]
        assert "WHY: Because reasons" in text

    async def test_omits_why_when_no_detail(self) -> None:
        gw = _make_gateway()
        sent = MagicMock()
        sent.message_id = 200
        gw._bot.send_message = AsyncMock(return_value=sent)

        proposal = _make_proposal(detail="")
        settings = _settings()

        with patch("theo.gates.proposals.get_settings", return_value=settings):
            await gw._on_proposal_created(proposal)

        text = gw._bot.send_message.call_args.kwargs["text"]
        assert "WHY:" not in text


class TestConcurrentCap:
    async def test_rejects_when_at_capacity(self) -> None:
        gw = _make_gateway()
        settings = _settings(max_pending_proposals=2)

        gw._pending["aaa"] = _make_proposal()
        gw._pending["bbb"] = _make_proposal()

        gw._bot.send_message = AsyncMock()

        with patch("theo.gates.proposals.get_settings", return_value=settings):
            await gw._on_proposal_created(_make_proposal())

        gw._bot.send_message.assert_called_once()
        text = gw._bot.send_message.call_args.kwargs["text"]
        assert "Cannot present new proposal" in text
        assert gw.pending_count == 2

    async def test_allows_when_under_capacity(self) -> None:
        gw = _make_gateway()
        settings = _settings(max_pending_proposals=5)

        sent = MagicMock()
        sent.message_id = 300
        gw._bot.send_message = AsyncMock(return_value=sent)

        with patch("theo.gates.proposals.get_settings", return_value=settings):
            await gw._on_proposal_created(_make_proposal())

        assert gw.pending_count == 1


class TestApproveCallback:
    async def test_publishes_approve_response(self) -> None:
        gw = _make_gateway()
        proposal = _make_proposal()
        short = _short_id(proposal.proposal_id)

        gw._pending[short] = proposal
        gw._messages[short] = 200
        gw._timers[short] = asyncio.create_task(asyncio.sleep(9999))

        published: list[ProposalResponse] = []

        async def capture(event: object) -> None:
            if isinstance(event, ProposalResponse):
                published.append(event)

        callback = _make_callback(f"proposal:{short}:approve")

        with patch.object(bus, "publish", side_effect=capture):
            await gw._on_callback(callback)

        callback.answer.assert_called_once_with(text="Approved!")
        assert len(published) == 1
        assert published[0].action == "approve"
        assert published[0].proposal_id == proposal.proposal_id
        assert short not in gw._pending

    async def test_edits_message_with_status_on_approve(self) -> None:
        gw = _make_gateway()
        gw._bot.edit_message_text = AsyncMock()
        proposal = _make_proposal()
        short = _short_id(proposal.proposal_id)

        gw._pending[short] = proposal
        gw._messages[short] = 200
        gw._message_texts[short] = "[EXTERNAL ACTION] Proposal\n\nWHAT: Test"
        gw._timers[short] = asyncio.create_task(asyncio.sleep(9999))

        with patch.object(bus, "publish", new_callable=AsyncMock):
            await gw._on_callback(_make_callback(f"proposal:{short}:approve"))

        gw._bot.edit_message_text.assert_called_once()
        call_kwargs = gw._bot.edit_message_text.call_args.kwargs
        assert call_kwargs["message_id"] == 200
        assert "(Approved)" in call_kwargs["text"]
        assert call_kwargs["reply_markup"] is None


class TestRejectCallback:
    async def test_publishes_reject_response(self) -> None:
        gw = _make_gateway()
        proposal = _make_proposal()
        short = _short_id(proposal.proposal_id)

        gw._pending[short] = proposal
        gw._messages[short] = 200
        gw._timers[short] = asyncio.create_task(asyncio.sleep(9999))

        published: list[ProposalResponse] = []

        async def capture(event: object) -> None:
            if isinstance(event, ProposalResponse):
                published.append(event)

        callback = _make_callback(f"proposal:{short}:reject")

        with patch.object(bus, "publish", side_effect=capture):
            await gw._on_callback(callback)

        callback.answer.assert_called_once_with(text="Rejected.")
        assert len(published) == 1
        assert published[0].action == "reject"
        assert short not in gw._pending


class TestModifyTap:
    async def test_sets_awaiting_modification(self) -> None:
        gw = _make_gateway()
        proposal = _make_proposal()
        short = _short_id(proposal.proposal_id)

        gw._pending[short] = proposal
        gw._messages[short] = 200
        gw._timers[short] = asyncio.create_task(asyncio.sleep(9999))

        callback = _make_callback(f"proposal:{short}:modify")
        await gw._on_callback(callback)

        callback.answer.assert_called_once_with(
            text="Reply to this message with your modification",
        )
        assert gw._awaiting_modification[200] == short

    async def test_does_not_remove_from_pending(self) -> None:
        gw = _make_gateway()
        proposal = _make_proposal()
        short = _short_id(proposal.proposal_id)

        gw._pending[short] = proposal
        gw._messages[short] = 200
        gw._timers[short] = asyncio.create_task(asyncio.sleep(9999))

        await gw._on_callback(_make_callback(f"proposal:{short}:modify"))
        assert short in gw._pending


class TestModifyViaReply:
    async def test_handles_reply_to_proposal(self) -> None:
        gw = _make_gateway()
        proposal = _make_proposal()
        short = _short_id(proposal.proposal_id)

        gw._pending[short] = proposal
        gw._messages[short] = 200
        gw._timers[short] = asyncio.create_task(asyncio.sleep(9999))
        gw._awaiting_modification[200] = short

        published: list[ProposalResponse] = []

        async def capture(event: object) -> None:
            if isinstance(event, ProposalResponse):
                published.append(event)

        reply = _make_reply_message(200, text="Do it differently")

        with patch.object(bus, "publish", side_effect=capture):
            handled = await gw.try_handle_reply(reply)

        assert handled is True
        assert len(published) == 1
        assert published[0].action == "modify"
        assert published[0].modification == "Do it differently"
        assert short not in gw._pending

    async def test_non_reply_returns_false(self) -> None:
        gw = _make_gateway()
        msg = MagicMock()
        msg.reply_to_message = None

        handled = await gw.try_handle_reply(msg)
        assert handled is False

    async def test_reply_to_non_proposal_returns_false(self) -> None:
        gw = _make_gateway()
        reply = _make_reply_message(999)

        handled = await gw.try_handle_reply(reply)
        assert handled is False

    async def test_reply_cleans_up_state(self) -> None:
        gw = _make_gateway()
        proposal = _make_proposal()
        short = _short_id(proposal.proposal_id)

        gw._pending[short] = proposal
        gw._messages[short] = 200
        timer = asyncio.create_task(asyncio.sleep(9999))
        gw._timers[short] = timer
        gw._awaiting_modification[200] = short

        with patch.object(bus, "publish", new_callable=AsyncMock):
            await gw.try_handle_reply(_make_reply_message(200))

        assert short not in gw._pending
        assert short not in gw._timers
        assert timer.cancelling()


class TestExpiredCallback:
    async def test_expired_proposal_answers_callback(self) -> None:
        gw = _make_gateway()
        callback = _make_callback("proposal:deadbeef:approve")

        await gw._on_callback(callback)

        callback.answer.assert_called_once()
        assert "expired" in callback.answer.call_args.kwargs["text"].lower()


class TestTimeout:
    async def test_expire_publishes_event(self) -> None:
        gw = _make_gateway()
        proposal = _make_proposal(timeout_s=0)
        short = _short_id(proposal.proposal_id)

        gw._pending[short] = proposal
        gw._messages[short] = 200

        published: list[ProposalExpired] = []

        async def capture(event: object) -> None:
            if isinstance(event, ProposalExpired):
                published.append(event)

        with patch.object(bus, "publish", side_effect=capture):
            await gw._expire_after(short, 0)

        assert len(published) == 1
        assert published[0].proposal_id == proposal.proposal_id
        assert short not in gw._pending

    async def test_expire_edits_message_with_status(self) -> None:
        gw = _make_gateway()
        gw._bot.edit_message_text = AsyncMock()
        proposal = _make_proposal()
        short = _short_id(proposal.proposal_id)

        gw._pending[short] = proposal
        gw._messages[short] = 200
        gw._message_texts[short] = "[EXTERNAL ACTION] Proposal\n\nWHAT: Test"

        with patch.object(bus, "publish", new_callable=AsyncMock):
            await gw._expire_after(short, 0)

        gw._bot.edit_message_text.assert_called_once()
        call_kwargs = gw._bot.edit_message_text.call_args.kwargs
        assert call_kwargs["message_id"] == 200
        assert "(Expired)" in call_kwargs["text"]
        assert call_kwargs["reply_markup"] is None

    async def test_expire_already_handled_is_noop(self) -> None:
        gw = _make_gateway()
        published: list[object] = []

        async def capture(event: object) -> None:
            published.append(event)

        with patch.object(bus, "publish", side_effect=capture):
            await gw._expire_after("nonexist", 0)

        assert len(published) == 0


class TestShutdown:
    async def test_cancels_all_timers(self) -> None:
        gw = _make_gateway()
        t1 = asyncio.create_task(asyncio.sleep(9999))
        t2 = asyncio.create_task(asyncio.sleep(9999))
        gw._timers["aaa"] = t1
        gw._timers["bbb"] = t2

        await gw.shutdown()

        assert t1.cancelling()
        assert t2.cancelling()
        assert len(gw._timers) == 0


class TestTelegramGateProposalIntegration:
    def test_gate_creates_proposal_gateway(self) -> None:
        from theo.gates.telegram import TelegramGate

        settings = _settings(
            telegram_bot_token=_TEST_TOKEN,
            telegram_owner_chat_id=_OWNER_CHAT_ID,
        )
        with patch("theo.gates.telegram.get_settings", return_value=settings):
            gate = TelegramGate()

        assert gate._proposals is not None
        assert isinstance(gate._proposals, ProposalGateway)

    async def test_reply_to_proposal_not_published_as_message(self) -> None:
        """When owner replies to a proposal, it should NOT also become a MessageReceived."""
        from theo.gates.telegram import TelegramGate

        settings = _settings(
            telegram_bot_token=_TEST_TOKEN,
            telegram_owner_chat_id=_OWNER_CHAT_ID,
        )
        with patch("theo.gates.telegram.get_settings", return_value=settings):
            gate = TelegramGate()

        proposal = _make_proposal()
        short = _short_id(proposal.proposal_id)

        gate._proposals._pending[short] = proposal
        gate._proposals._messages[short] = 500
        gate._proposals._timers[short] = asyncio.create_task(asyncio.sleep(9999))
        gate._proposals._awaiting_modification[500] = short

        msg = MagicMock()
        msg.chat.id = _OWNER_CHAT_ID
        msg.text = "Do it this way instead"
        msg.message_id = 501
        msg.voice = None
        msg.audio = None
        msg.reply_to_message = MagicMock()
        msg.reply_to_message.message_id = 500

        published: list[object] = []

        async def capture(event: object) -> None:
            published.append(event)

        with patch.object(bus, "publish", side_effect=capture):
            await gate._on_message(msg)

        assert len(published) == 1
        assert isinstance(published[0], ProposalResponse)
        assert published[0].action == "modify"

    async def test_normal_reply_still_works(self) -> None:
        """A reply to a non-proposal message should be processed normally."""
        from theo.bus.events import MessageReceived
        from theo.gates.telegram import TelegramGate

        settings = _settings(
            telegram_bot_token=_TEST_TOKEN,
            telegram_owner_chat_id=_OWNER_CHAT_ID,
        )
        with patch("theo.gates.telegram.get_settings", return_value=settings):
            gate = TelegramGate()

        msg = MagicMock()
        msg.chat.id = _OWNER_CHAT_ID
        msg.text = "Just a normal reply"
        msg.message_id = 600
        msg.voice = None
        msg.audio = None
        msg.reply_to_message = MagicMock()
        msg.reply_to_message.message_id = 999

        published: list[object] = []

        async def capture(event: object) -> None:
            published.append(event)

        with patch.object(bus, "publish", side_effect=capture):
            await gate._on_message(msg)

        assert len(published) == 1
        assert isinstance(published[0], MessageReceived)
