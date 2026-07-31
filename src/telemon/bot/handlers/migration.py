"""Chat migration service-message handler.

Telegram announces a group -> supergroup upgrade twice: a service message in
the *old* chat carrying ``migrate_to_chat_id``, and one in the *new* chat
carrying ``migrate_from_chat_id``.  Either is enough to re-key immediately,
before the next spawn attempt discovers the dead id the hard way.

This router is registered before ``spawn.router`` because that one swallows
every group message, and service messages have no ``text`` so they would be
dropped by its anti-spam filter without ever reaching here.
"""

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.core.migration import migrate_chat
from telemon.logging import get_logger

router = Router(name="migration")
logger = get_logger(__name__)


@router.message(F.migrate_to_chat_id)
async def on_migrate_to(message: Message, session: AsyncSession) -> None:
    """Service message in the old chat: it is becoming ``migrate_to_chat_id``."""
    await migrate_chat(session, message.chat.id, message.migrate_to_chat_id)


@router.message(F.migrate_from_chat_id)
async def on_migrate_from(message: Message, session: AsyncSession) -> None:
    """Service message in the new supergroup: it used to be ``migrate_from_chat_id``."""
    await migrate_chat(session, message.migrate_from_chat_id, message.chat.id)
