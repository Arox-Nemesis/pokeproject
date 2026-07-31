"""Telegram chat migration (group -> supergroup) re-keying.

When a Telegram group is upgraded to a supergroup its ``chat_id`` changes --
a positive/small negative id becomes a ``-100...`` one -- and every subsequent
send to the old id fails with ``TelegramMigrateToChat``, which carries the new
id in ``migrate_to_chat_id``.

That exception is *not* a subclass of ``TelegramBadRequest`` (its MRO is
``TelegramMigrateToChat -> TelegramAPIError -> DetailedAiogramError ->
AiogramError``), so every send path in this codebase used to fall into its
generic "transient" branch and retry the dead id forever: the group looked
alive, spawns kept being created and deleted, and no message ever arrived.

Five columns key on a chat id and must all move together:

* ``groups.chat_id``          (primary key)
* ``active_spawns.chat_id``
* ``battles.chat_id``
* ``trades.chat_id``
* ``pokemon.caught_in_group_id``

No foreign key references ``groups``, so the primary key can be updated in
place without cascade concerns.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.database.models import ActiveSpawn, Battle, Group, Pokemon, Trade
from telemon.logging import get_logger

logger = get_logger(__name__)


async def migrate_chat(
    session: AsyncSession, old_chat_id: int, new_chat_id: int
) -> bool:
    """Re-key every row from ``old_chat_id`` to ``new_chat_id``.

    Idempotent: calling it again after a successful migration is a no-op,
    because nothing is left under the old id.  Returns True if anything moved.

    If a ``groups`` row already exists under the new id -- the bot can be
    re-added to the upgraded supergroup before the old id is ever touched
    again -- the two rows are merged: counters are summed, the newer settings
    win, and the stale row is dropped.
    """
    if old_chat_id == new_chat_id:
        return False

    old_group = await session.get(Group, old_chat_id)
    new_group = await session.get(Group, new_chat_id)

    if old_group is not None and new_group is not None:
        # Both rows exist -- fold the old stats into the surviving row.
        new_group.total_spawns += old_group.total_spawns
        new_group.total_catches += old_group.total_catches
        new_group.message_count = max(new_group.message_count, old_group.message_count)
        if old_group.last_spawn_at and (
            new_group.last_spawn_at is None
            or old_group.last_spawn_at > new_group.last_spawn_at
        ):
            new_group.last_spawn_at = old_group.last_spawn_at
        if old_group.bot_joined_at and (
            new_group.bot_joined_at is None
            or old_group.bot_joined_at < new_group.bot_joined_at
        ):
            # Keep the earliest join time -- it gates time-based spawning.
            new_group.bot_joined_at = old_group.bot_joined_at
        if old_group.is_banned:
            new_group.is_banned = True
            new_group.ban_reason = new_group.ban_reason or old_group.ban_reason
        new_group.is_premium = new_group.is_premium or old_group.is_premium
        await session.delete(old_group)
    elif old_group is not None:
        old_group.chat_id = new_chat_id

    for model, column in (
        (ActiveSpawn, ActiveSpawn.chat_id),
        (Battle, Battle.chat_id),
        (Trade, Trade.chat_id),
        (Pokemon, Pokemon.caught_in_group_id),
    ):
        await session.execute(
            update(model).where(column == old_chat_id).values({column: new_chat_id})
        )

    await session.commit()
    logger.info(
        "Migrated chat id",
        old_chat_id=old_chat_id,
        new_chat_id=new_chat_id,
        merged=old_group is not None and new_group is not None,
    )
    return True


async def find_stale_migrations(session: AsyncSession) -> list[int]:
    """Return group chat ids that predate the supergroup id format.

    Only useful for reporting: the new id is not derivable from the old one,
    Telegram only ever hands it over in a ``migrate_to_chat_id`` field.
    """
    result = await session.execute(
        select(Group.chat_id).where(Group.chat_id > -1000000000000)
    )
    return [cid for cid in result.scalars().all() if cid < 0]
