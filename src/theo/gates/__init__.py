"""External interface gates (Telegram, future: email, web, API)."""

from theo.gates.telegram import TelegramGate, escape_markdownv2, session_id_for_chat, split_message

__all__ = ["TelegramGate", "escape_markdownv2", "session_id_for_chat", "split_message"]
