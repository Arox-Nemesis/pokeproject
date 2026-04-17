"""Wonder Trade handler — anonymous Pokemon exchange."""

from datetime import datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.core.emoji import poke_emoji
from telemon.database.models import Pokemon, User, WonderTrade
from telemon.logging import get_logger

router = Router(name="wondertrade")
logger = get_logger(__name__)

# Cooldown: 1 wonder trade per 5 minutes per user
WT_COOLDOWN_SECONDS = 300
_wt_cooldowns: dict[int, datetime] = {}


async def _get_user_pokemon_by_index(
    session: AsyncSession, user_id: int, index: int
) -> Pokemon | None:
    """Get a user's Pokemon by 1-based index (sorted by caught_at asc)."""
    result = await session.execute(
        select(Pokemon)
        .where(Pokemon.owner_id == user_id)
        .order_by(Pokemon.caught_at.asc())
    )
    pokemon_list = list(result.scalars().all())
    if index < 1 or index > len(pokemon_list):
        return None
    return pokemon_list[index - 1]


@router.message(Command("wondertrade", "wt"))
async def cmd_wonder_trade(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /wondertrade command."""
    text = message.text or ""
    args = text.split()

    if len(args) < 2:
        # Show wonder trade info + status
        await show_wt_status(message, session, user)
        return

    sub = args[1].lower()

    if sub == "help":
        await wt_help(message)
        return

    if sub == "status":
        await show_wt_status(message, session, user)
        return

    # Try to parse as Pokemon index
    if sub.isdigit():
        pokemon_idx = int(sub)
        await do_wonder_trade(message, session, user, pokemon_idx)
        return

    await message.answer(
        "Usage: /wt [pokemon#]\n"
        "Example: /wt 5 — Wonder Trade your Pokemon #5\n\n"
        "Use /wt for more info."
    )


async def show_wt_status(message: Message, session: AsyncSession, user: User) -> None:
    """Show Wonder Trade status and info."""
    # Count waiting Pokemon in pool
    pool_count_result = await session.execute(
        select(func.count(WonderTrade.id)).where(WonderTrade.is_matched == False)
    )
    pool_count = pool_count_result.scalar() or 0

    # Check if user has a Pokemon in the pool
    user_wt_result = await session.execute(
        select(WonderTrade)
        .where(WonderTrade.user_id == user.telegram_id, WonderTrade.is_matched == False)
    )
    user_pending = user_wt_result.scalar_one_or_none()

    # Total trades completed
    total_result = await session.execute(
        select(func.count(WonderTrade.id)).where(WonderTrade.is_matched == True)
    )
    total_trades = (total_result.scalar() or 0) // 2  # Each trade = 2 entries

    lines = [
        "<b>Wonder Trade</b>\n",
        "Deposit a Pokemon and receive a random one from another trainer!",
        f"\n<b>Pool:</b> {pool_count} Pokemon waiting",
        f"<b>Total trades:</b> {total_trades:,}",
    ]

    if user_pending:
        lines.append(
            f"\n⏳ You have <b>{user_pending.species_name}</b> (Lv.{user_pending.level}) waiting in the pool."
        )
        lines.append("It will be traded when another trainer deposits a Pokemon!")
    else:
        lines.append(
            "\n<b>How to trade:</b>\n"
            "/wt [pokemon#] — Deposit a Pokemon\n"
            "If someone is waiting, you swap instantly!\n"
            "If not, your Pokemon waits for the next trader."
        )

    # Cooldown check
    now = datetime.utcnow()
    if user.telegram_id in _wt_cooldowns:
        elapsed = (now - _wt_cooldowns[user.telegram_id]).total_seconds()
        if elapsed < WT_COOLDOWN_SECONDS:
            remaining = int(WT_COOLDOWN_SECONDS - elapsed)
            mins = remaining // 60
            secs = remaining % 60
            lines.append(f"\nCooldown: {mins}m {secs}s")

    await message.answer("\n".join(lines))


async def do_wonder_trade(
    message: Message, session: AsyncSession, user: User, pokemon_idx: int
) -> None:
    """Execute a wonder trade."""
    now = datetime.utcnow()

    # Cooldown check
    if user.telegram_id in _wt_cooldowns:
        elapsed = (now - _wt_cooldowns[user.telegram_id]).total_seconds()
        if elapsed < WT_COOLDOWN_SECONDS:
            remaining = int(WT_COOLDOWN_SECONDS - elapsed)
            mins = remaining // 60
            secs = remaining % 60
            await message.answer(f"Wonder Trade cooldown! Wait {mins}m {secs}s.")
            return

    # Check if user already has a Pokemon in the pool
    existing_result = await session.execute(
        select(WonderTrade)
        .where(WonderTrade.user_id == user.telegram_id, WonderTrade.is_matched == False)
    )
    if existing_result.scalar_one_or_none():
        await message.answer(
            "You already have a Pokemon in the Wonder Trade pool!\n"
            "Wait for it to be traded first."
        )
        return

    # Get the Pokemon
    poke = await _get_user_pokemon_by_index(session, user.telegram_id, pokemon_idx)
    if not poke:
        await message.answer(f"Pokemon #{pokemon_idx} not found! Check /pokemon for your list.")
        return

    # Validate
    if poke.is_favorite:
        await message.answer(
            f"{poke.display_name} is a favorite! Remove from favorites first (/fav {pokemon_idx})"
        )
        return

    if poke.is_on_market:
        await message.answer(f"{poke.display_name} is listed on the market!")
        return

    if poke.is_in_trade:
        await message.answer(f"{poke.display_name} is in an active trade!")
        return

    if str(poke.id) == user.selected_pokemon_id:
        await message.answer(f"{poke.display_name} is your selected Pokemon! Select another first.")
        return

    # Check for a waiting trade from a DIFFERENT user
    match_result = await session.execute(
        select(WonderTrade)
        .where(
            WonderTrade.is_matched == False,
            WonderTrade.user_id != user.telegram_id,
        )
        .order_by(WonderTrade.created_at.asc())
        .limit(1)
    )
    match = match_result.scalar_one_or_none()

    if match:
        # Instant match! Swap Pokemon
        match_pokemon_result = await session.execute(
            select(Pokemon).where(Pokemon.id == match.pokemon_id)
        )
        match_pokemon = match_pokemon_result.scalar_one_or_none()

        if not match_pokemon:
            # Stale entry, remove it and deposit instead
            await session.delete(match)
            await _deposit_pokemon(session, user, poke, message)
            return

        # Swap ownership
        old_owner_id = match_pokemon.owner_id
        match_pokemon.owner_id = user.telegram_id
        match_pokemon.original_trainer_id = match_pokemon.original_trainer_id or old_owner_id
        poke.owner_id = old_owner_id
        poke.original_trainer_id = poke.original_trainer_id or user.telegram_id

        # Create the depositor's WT entry
        user_wt = WonderTrade(
            user_id=user.telegram_id,
            pokemon_id=poke.id,
            species_id=poke.species_id,
            species_name=poke.species.name,
            level=poke.level,
            is_shiny=poke.is_shiny,
            is_matched=True,
            matched_with_id=match.id,
            matched_at=now,
        )
        session.add(user_wt)

        # Mark the waiting entry as matched
        match.is_matched = True
        match.matched_with_id = user_wt.id
        match.matched_at = now

        # Set cooldown
        _wt_cooldowns[user.telegram_id] = now

        await session.commit()

        # Update quest progress
        from telemon.core.quests import update_quest_progress
        await update_quest_progress(session, user.telegram_id, "trade")
        await update_quest_progress(session, old_owner_id, "trade")

        # Increment trade counters
        user.total_trades += 1
        # Also increment for the other user
        from telemon.database.models import User as UserModel
        other_user_result = await session.execute(
            select(UserModel).where(UserModel.telegram_id == old_owner_id)
        )
        other_user = other_user_result.scalar_one_or_none()
        if other_user:
            other_user.total_trades += 1

        # Achievement hooks
        from telemon.core.achievements import check_achievements, format_achievement_notification
        wt_achs = await check_achievements(session, user.telegram_id, "wonder_trade")
        wt_achs.extend(await check_achievements(session, user.telegram_id, "trade"))
        wt_achs.extend(await check_achievements(session, old_owner_id, "wonder_trade"))
        wt_achs.extend(await check_achievements(session, old_owner_id, "trade"))
        await session.commit()

        wt_ach_text = format_achievement_notification(wt_achs)

        # Build response for the depositor
        received = match_pokemon
        shiny_sent = " ✨" if poke.is_shiny else ""
        shiny_recv = " ✨" if received.is_shiny else ""

        sprite_sent = poke_emoji(poke.species.national_dex)
        sprite_recv = poke_emoji(received.species.national_dex)

        await message.answer(
            f"<b>Wonder Trade Complete!</b>\n\n"
            f"<b>Sent:</b> {sprite_sent}{poke.species.name}{shiny_sent} (Lv.{poke.level})\n"
            f"<b>Received:</b> {sprite_recv}{received.species.name}{shiny_recv} (Lv.{received.level})\n"
            f"IV: {received.iv_percentage:.1f}% | Nature: {received.nature.title()}\n\n"
            f"<i>The other trainer sent you their {received.species.name}!</i>"
            f"{wt_ach_text}"
        )

        logger.info(
            "Wonder Trade matched",
            user1=user.telegram_id,
            user2=old_owner_id,
            sent=poke.species.name,
            received=received.species.name,
        )

        # DM notify the other user about the wonder trade match
        try:
            from telemon.core.notifications import notify_wonder_trade_match
            await notify_wonder_trade_match(
                bot=message.bot,
                user_id=old_owner_id,
                sent_name=received.species.name,  # What they sent
                received_name=poke.species.name,   # What they got back
                received_level=poke.level,
                received_iv=poke.iv_percentage,
                is_shiny=poke.is_shiny,
            )
        except Exception:
            pass  # Best-effort DM
    else:
        # No match — deposit into pool
        await _deposit_pokemon(session, user, poke, message)


async def _deposit_pokemon(
    session: AsyncSession, user: User, poke: Pokemon, message: Message
) -> None:
    """Deposit a Pokemon into the Wonder Trade pool."""
    now = datetime.utcnow()

    wt = WonderTrade(
        user_id=user.telegram_id,
        pokemon_id=poke.id,
        species_id=poke.species_id,
        species_name=poke.species.name,
        level=poke.level,
        is_shiny=poke.is_shiny,
    )
    session.add(wt)

    # Set cooldown
    _wt_cooldowns[user.telegram_id] = now

    await session.commit()

    shiny = " ✨" if poke.is_shiny else ""
    sprite = poke_emoji(poke.species.national_dex)
    await message.answer(
        f"<b>Wonder Trade — Pokemon Deposited!</b>\n\n"
        f"{sprite}<b>{poke.species.name}</b>{shiny} (Lv.{poke.level}) is now in the pool.\n\n"
        f"When another trainer deposits a Pokemon, you'll swap instantly!\n"
        f"<i>Check back with /wt status</i>"
    )

    logger.info(
        "Pokemon deposited in Wonder Trade",
        user_id=user.telegram_id,
        pokemon=poke.species.name,
        level=poke.level,
    )


async def wt_help(message: Message) -> None:
    """Show Wonder Trade help."""
    await message.answer(
        "<b>Wonder Trade</b>\n\n"
        "Deposit a Pokemon and receive a random one from another trainer!\n\n"
        "<b>Commands:</b>\n"
        "/wt [pokemon#] — Deposit a Pokemon\n"
        "/wt status — Check pool status\n"
        "/wt help — Show this help\n\n"
        "<b>How it works:</b>\n"
        "1. Use /wt [number] to deposit a Pokemon\n"
        "2. If someone is waiting, you swap instantly!\n"
        "3. If not, your Pokemon waits in the pool\n"
        "4. The next trader will match with you\n\n"
        "<b>Rules:</b>\n"
        "- You can only have 1 Pokemon in the pool at a time\n"
        "- Favorites and selected Pokemon can't be traded\n"
        "- 5 minute cooldown between trades\n"
        "- You can't match with yourself"
    )
