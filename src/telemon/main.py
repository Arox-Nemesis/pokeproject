"""Main entry point for PokeVault bot."""

import asyncio
import sys

from telemon.bot import create_bot, create_dispatcher
from telemon.database import close_db, init_db
from telemon.logging import get_logger, setup_logging

logger = get_logger(__name__)


async def timed_spawn_loop(bot) -> None:
    """Background task: periodically spawn Pokemon in active groups.

    Each spawn-enabled group gets a random no-activity spawn interval
    between 10-20 minutes.  The loop checks every 60 seconds and fires
    a timed spawn if the group's interval has elapsed since its last spawn
    and there is no active (uncaught) spawn.
    """
    import random
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from telemon.database import async_session_factory
    from telemon.database.models import Group
    from telemon.core.spawning import create_spawn, get_random_species, get_active_spawn

    await asyncio.sleep(60)  # Wait 1 minute after startup

    # Per-group random interval (minutes) — re-rolled after each timed spawn
    _group_intervals: dict[int, float] = {}

    def _get_interval(chat_id: int) -> float:
        if chat_id not in _group_intervals:
            _group_intervals[chat_id] = random.uniform(10, 20)
        return _group_intervals[chat_id]

    def _reroll_interval(chat_id: int) -> None:
        _group_intervals[chat_id] = random.uniform(10, 20)

    while True:
        try:
            async with async_session_factory() as session:
                result = await session.execute(
                    select(Group).where(Group.spawn_enabled == True)
                )
                groups = result.scalars().all()

                now = datetime.utcnow()

                for group in groups:
                    interval_mins = _get_interval(group.chat_id)
                    cutoff = now - timedelta(minutes=interval_mins)

                    # Skip groups that spawned recently
                    if group.last_spawn_at and group.last_spawn_at > cutoff:
                        continue

                    # Skip if there's already an active spawn
                    active = await get_active_spawn(session, group.chat_id)
                    if active:
                        continue

                    # Only spawn in groups that have had at least some activity
                    if group.total_spawns == 0 and group.message_count < 5:
                        continue

                    species = await get_random_species(session)
                    if not species:
                        continue

                    spawn = await create_spawn(
                        session=session,
                        chat_id=group.chat_id,
                        message_id=0,
                        species=species,
                    )

                    if spawn:
                        from telemon.bot.handlers.spawn import send_spawn_message
                        msg_id = await send_spawn_message(bot, group.chat_id, spawn)
                        if msg_id:
                            spawn.message_id = msg_id

                            # Only update spawn stats after confirmed delivery
                            group.total_spawns += 1
                            group.last_spawn_at = datetime.utcnow()

                            await session.commit()
                            logger.info(
                                "Timed spawn triggered",
                                chat_id=group.chat_id,
                                species=species.name,
                                interval_min=round(interval_mins, 1),
                            )
                        else:
                            # send_spawn_message returned None — likely
                            # "chat not found" or "bot was kicked".
                            # Auto-disable spawns for this group so we stop
                            # hammering a dead chat.
                            await session.delete(spawn)
                            group.spawn_enabled = False
                            await session.commit()
                            logger.warning(
                                "Auto-disabled spawns for unreachable group",
                                chat_id=group.chat_id,
                            )
                        # Re-roll interval for next time
                        _reroll_interval(group.chat_id)

        except Exception as e:
            logger.error("Error in timed spawn loop", error=str(e))

        # Check every 60 seconds
        await asyncio.sleep(60)


async def incense_dm_spawn_loop(bot) -> None:
    """Background task: spawn Pokemon in DMs for users with active Incense.

    Checks every 30 seconds.  Each user gets a random spawn interval
    between 2-5 minutes, re-rolled after each spawn.
    """
    import random
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from telemon.database import async_session_factory
    from telemon.database.models import User
    from telemon.core.spawning import create_spawn, get_random_species, get_active_spawn

    await asyncio.sleep(30)  # Wait after startup

    # Per-user last DM spawn time + next interval
    _user_state: dict[int, tuple[datetime, float]] = {}  # uid -> (last_spawn, interval_min)
    _MAX_STATE_SIZE = 500

    while True:
        try:
            async with async_session_factory() as session:
                now = datetime.utcnow()

                # Find users with active incense
                result = await session.execute(
                    select(User).where(User.incense_until > now)
                )
                users = result.scalars().all()

                for user in users:
                    uid = user.telegram_id

                    # Get or create per-user spawn state
                    if uid not in _user_state:
                        _user_state[uid] = (now, random.uniform(2, 5))

                    last_spawn, interval_min = _user_state[uid]
                    cutoff = last_spawn + timedelta(minutes=interval_min)

                    if now < cutoff:
                        continue  # Not time yet

                    # Check for existing active spawn in DM
                    active = await get_active_spawn(session, uid)
                    if active:
                        continue

                    # Spawn a Pokemon in the user's DM
                    species = await get_random_species(session)
                    if not species:
                        continue

                    spawn = await create_spawn(
                        session=session,
                        chat_id=uid,  # DM chat_id == user's telegram_id
                        message_id=0,
                        species=species,
                    )

                    if spawn:
                        from telemon.bot.handlers.spawn import send_spawn_message
                        msg_id = await send_spawn_message(bot, uid, spawn)
                        if msg_id:
                            spawn.message_id = msg_id
                            await session.commit()
                            logger.info(
                                "Incense DM spawn",
                                user_id=uid,
                                species=species.name,
                            )
                        else:
                            await session.delete(spawn)
                            await session.commit()
                            logger.debug(
                                "Incense DM spawn failed to send",
                                user_id=uid,
                            )

                    # Re-roll interval
                    _user_state[uid] = (now, random.uniform(2, 5))

                # Prune expired users from state
                expired = [uid for uid in _user_state if uid not in {u.telegram_id for u in users}]
                for uid in expired:
                    del _user_state[uid]

                # Cap state size
                if len(_user_state) > _MAX_STATE_SIZE:
                    oldest = sorted(_user_state, key=lambda u: _user_state[u][0])
                    for uid in oldest[:len(_user_state) - _MAX_STATE_SIZE]:
                        del _user_state[uid]

        except Exception as e:
            logger.error("Error in incense DM spawn loop", error=str(e))

        await asyncio.sleep(30)


async def trade_expiry_loop(bot) -> None:
    """Background task: auto-cancel trades after 5 minutes of inactivity."""
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from telemon.database import async_session_factory
    from telemon.database.models import Pokemon
    from telemon.database.models.trade import Trade, TradeStatus

    await asyncio.sleep(30)  # Wait 30 seconds after startup

    while True:
        try:
            async with async_session_factory() as session:
                cutoff = datetime.utcnow() - timedelta(minutes=5)

                # Use last_activity_at if available, fall back to created_at
                # (last_activity_at requires an Alembic migration that may
                # not have been applied yet).
                try:
                    result = await session.execute(
                        select(Trade).where(
                            Trade.status.in_([
                                TradeStatus.WAITING_ACCEPT,
                                TradeStatus.PENDING,
                                TradeStatus.CONFIRMED_ONE,
                            ]),
                            Trade.last_activity_at < cutoff,
                        )
                    )
                except Exception:
                    # Column doesn't exist yet — roll back the aborted
                    # transaction before running the fallback query.
                    await session.rollback()
                    result = await session.execute(
                        select(Trade).where(
                            Trade.status.in_([
                                TradeStatus.WAITING_ACCEPT,
                                TradeStatus.PENDING,
                                TradeStatus.CONFIRMED_ONE,
                            ]),
                            Trade.created_at < cutoff,
                        )
                    )
                expired_trades = result.scalars().all()

                for trade in expired_trades:
                    # Unmark all Pokemon in the trade
                    for poke_id in (trade.user1_pokemon_ids or []) + (trade.user2_pokemon_ids or []):
                        poke_result = await session.execute(
                            select(Pokemon).where(Pokemon.id == poke_id)
                        )
                        poke = poke_result.scalar_one_or_none()
                        if poke:
                            poke.is_in_trade = False

                    trade.status = TradeStatus.CANCELLED
                    await session.commit()

                    logger.info(
                        "Trade auto-cancelled due to inactivity",
                        trade_id=str(trade.id),
                        user1_id=trade.user1_id,
                        user2_id=trade.user2_id,
                    )

                    # Notify the chat
                    if trade.chat_id:
                        try:
                            await bot.send_message(
                                trade.chat_id,
                                "⏳ <b>Trade Auto-Cancelled</b>\n\n"
                                "The trade was cancelled due to 5 minutes of inactivity.\n"
                                "All Pok\u00e9mon have been returned.",
                            )
                        except Exception:
                            pass  # Chat may no longer be accessible

        except Exception as e:
            logger.error("Error in trade expiry loop", error=str(e))

        await asyncio.sleep(60)


async def main() -> None:
    """Main function to run the bot."""
    # Set up logging
    setup_logging()
    logger.info("Starting PokeVault bot...")

    # Initialize database
    try:
        await init_db()
        logger.info("Database connection established")
    except Exception as e:
        logger.error("Failed to connect to database", error=str(e))
        sys.exit(1)

    # Create bot and dispatcher
    bot = create_bot()
    dp = await create_dispatcher()

    try:
        # Get bot info
        bot_info = await bot.get_me()

        # Detect emoji mode based on bot ID
        from telemon.core.emoji import init_emoji, mode_label, total_emoji_count

        init_emoji(bot_info.id)
        counts = total_emoji_count()
        logger.info(
            "Emoji mode configured",
            mode=mode_label(),
            pokemon_emoji=counts["pokemon"],
            item_emoji=counts["items"],
            type_emoji=counts["types"],
        )

        logger.info(
            "Bot started",
            username=bot_info.username,
            bot_id=bot_info.id,
        )

        # Start background tasks
        spawn_task = asyncio.create_task(timed_spawn_loop(bot))
        incense_task = asyncio.create_task(incense_dm_spawn_loop(bot))
        trade_expiry_task = asyncio.create_task(trade_expiry_loop(bot))

        # Register global error handler for rate limiting
        from aiogram.exceptions import TelegramRetryAfter
        from aiogram.types.error_event import ErrorEvent

        @dp.errors()
        async def handle_rate_limit(event: ErrorEvent):
            if isinstance(event.exception, TelegramRetryAfter):
                logger.warning(
                    "Rate limited by Telegram",
                    retry_after=event.exception.retry_after,
                )
                return True  # Suppress the error
            return False

        # Start polling
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

    except Exception as e:
        logger.error("Bot error", error=str(e))
        raise
    finally:
        # Cleanup
        spawn_task.cancel()
        incense_task.cancel()
        trade_expiry_task.cancel()
        await bot.session.close()
        await close_db()
        logger.info("Bot stopped")


def run() -> None:
    """Entry point for the application."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
