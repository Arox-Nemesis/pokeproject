"""Auto-delete utility for transient bot messages.

In group chats, error messages and short-lived feedback clutter the
conversation.  ``reply_and_delete`` sends a reply that silently
self-destructs after a configurable delay, keeping the chat clean.
"""

import asyncio

from aiogram import Bot
from aiogram.types import Message

from telemon.logging import get_logger

logger = get_logger(__name__)

DEFAULT_DELETE_DELAY = 12  # seconds


async def _safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    """Delete a message, ignoring errors (already deleted, no perms, etc.)."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # Best-effort; the message may already be gone


async def reply_and_delete(
    message: Message,
    text: str,
    delay: int = DEFAULT_DELETE_DELAY,
    **kwargs,
) -> Message:
    """Send a reply that auto-deletes after *delay* seconds in group chats.

    In private chats the message is sent normally and never deleted
    (the user might want to scroll back).

    Extra *kwargs* are forwarded to ``message.answer()``.
    """
    sent = await message.answer(text, **kwargs)

    if message.chat.type in ("group", "supergroup"):
        asyncio.get_event_loop().call_later(
            delay,
            lambda: asyncio.ensure_future(
                _safe_delete(sent.bot, sent.chat.id, sent.message_id)
            ),
        )

    return sent
