"""Profile and user-related handlers."""

from datetime import datetime, timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.config import settings, CURRENCY_NAME, CURRENCY_SHORT
from telemon.core.constants import MAX_FRIENDSHIP, MAX_LEVEL, MAX_GIFT_AMOUNT
from telemon.core.emoji import poke_emoji
from telemon.database.models import PokedexEntry, Pokemon, User
from telemon.logging import get_logger

router = Router(name="profile")
logger = get_logger(__name__)


@router.message(Command("profile"))
async def cmd_profile(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /profile command."""
    # Get actual stats
    poke_result = await session.execute(
        select(func.count(Pokemon.id)).where(Pokemon.owner_id == user.telegram_id)
    )
    pokemon_count = poke_result.scalar() or 0

    unique_result = await session.execute(
        select(func.count(PokedexEntry.species_id))
        .where(PokedexEntry.user_id == user.telegram_id)
        .where(PokedexEntry.caught == True)
    )
    unique_caught = unique_result.scalar() or 0

    shiny_result = await session.execute(
        select(func.count(Pokemon.id))
        .where(Pokemon.owner_id == user.telegram_id)
        .where(Pokemon.is_shiny == True)
    )
    shiny_count = shiny_result.scalar() or 0

    # Selected Pokemon info
    selected_text = "<i>None selected</i>"
    if user.selected_pokemon_id:
        sel_result = await session.execute(
            select(Pokemon)
            .where(Pokemon.id == user.selected_pokemon_id)
            .where(Pokemon.owner_id == user.telegram_id)
        )
        sel_poke = sel_result.scalar_one_or_none()
        if sel_poke:
            shiny_mark = " ✨" if sel_poke.is_shiny else ""
            sprite = poke_emoji(sel_poke.species.national_dex)
            selected_text = f"{sprite}{sel_poke.display_name}{shiny_mark} Lv.{sel_poke.level} | Friendship: {sel_poke.friendship}/{MAX_FRIENDSHIP}"

    profile_text = (
        f"<b>Trainer Profile</b>\n\n"
        f"<b>Name:</b> {user.display_name}\n"
        f"<b>Balance:</b> {user.balance:,} {CURRENCY_NAME}\n\n"
        f"<b>Pokemon Stats</b>\n"
        f"  Total: {pokemon_count} | Unique: {unique_caught} | Shinies: {shiny_count}\n\n"
        f"<b>Battle Stats</b>\n"
        f"  {user.battle_wins}W/{user.battle_losses}L ({user.win_rate:.1f}%) | Rating: {user.battle_rating}\n\n"
        f"<b>Selected:</b> {selected_text}\n"
        f"<b>Streak:</b> {user.daily_streak} days\n"
        f"<i>Trainer since {user.created_at.strftime('%b %d, %Y')}</i>"
    )
    await message.answer(profile_text)


@router.message(Command("balance", "bal"))
async def cmd_balance(message: Message, user: User) -> None:
    """Handle /balance command."""
    await message.answer(f"<b>Balance:</b> {user.balance:,} {CURRENCY_NAME}")


@router.message(Command("daily"))
async def cmd_daily(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /daily command for daily rewards."""
    now = datetime.utcnow()

    # Check if already claimed today
    if user.last_daily:
        time_since_last = now - user.last_daily
        if time_since_last < timedelta(hours=20):
            # Already claimed
            next_claim = user.last_daily + timedelta(hours=20)
            remaining = next_claim - now
            hours = int(remaining.total_seconds() // 3600)
            minutes = int((remaining.total_seconds() % 3600) // 60)
            await message.answer(
                f"You've already claimed your daily reward!\n"
                f"Next claim in: <b>{hours}h {minutes}m</b>"
            )
            return

        # Check if streak should reset (more than 48 hours)
        if time_since_last > timedelta(hours=48):
            user.daily_streak = 0

    # Calculate reward with streak bonus
    streak = min(user.daily_streak, settings.daily_streak_max)
    base_reward = settings.daily_reward_base
    streak_bonus = streak * settings.daily_streak_bonus
    total_reward = base_reward + streak_bonus

    # Update user
    user.balance += total_reward
    user.daily_streak += 1
    user.last_daily = now

    # Friendship bonus: selected Pokemon gets friendship + XP
    friendship_text = ""
    daily_xp_text = ""
    if user.selected_pokemon_id:
        sel_result = await session.execute(
            select(Pokemon)
            .where(Pokemon.id == user.selected_pokemon_id)
            .where(Pokemon.owner_id == user.telegram_id)
        )
        sel_poke = sel_result.scalar_one_or_none()
        if sel_poke and sel_poke.friendship < MAX_FRIENDSHIP:
            gain = 5
            if sel_poke.held_item and sel_poke.held_item.lower() == "soothe bell":
                gain *= 2
            old = sel_poke.friendship
            sel_poke.friendship = min(MAX_FRIENDSHIP, sel_poke.friendship + gain)
            actual = sel_poke.friendship - old
            friendship_text = f"\n{sel_poke.display_name}: +{actual} friendship ({sel_poke.friendship}/{MAX_FRIENDSHIP})"

        # XP from daily claim
        if sel_poke and sel_poke.level < MAX_LEVEL:
            from telemon.core.leveling import calculate_daily_xp, add_xp_to_pokemon, format_xp_message

            daily_xp = calculate_daily_xp(user.daily_streak)
            xp_added, levels_gained, learned_moves = await add_xp_to_pokemon(
                session, str(sel_poke.id), daily_xp
            )
            if xp_added > 0:
                daily_xp_text = "\n" + format_xp_message(sel_poke.display_name, xp_added, levels_gained, learned_moves)

    await session.commit()

    # Update quest progress for daily claim
    from telemon.core.quests import update_quest_progress

    daily_quest_msg = ""
    completed = await update_quest_progress(session, user.telegram_id, "daily_claim")
    if completed:
        await session.commit()
        for q in completed:
            daily_quest_msg += f"\n\U0001f4cb Quest complete: {q.description} (+{q.reward_coins:,} {CURRENCY_SHORT})"

    # Achievement hooks for daily streak
    from telemon.core.achievements import check_achievements, format_achievement_notification
    daily_achs = await check_achievements(session, user.telegram_id, "daily")
    daily_ach_text = format_achievement_notification(daily_achs)
    if daily_achs:
        await session.commit()

    streak_text = ""
    if streak > 0:
        streak_text = f"\nStreak bonus: +{streak_bonus} ({streak} days)"

    await message.answer(
        f"🎁 <b>Daily Claimed!</b> +{base_reward}{streak_text} = <b>{total_reward}</b> {CURRENCY_NAME}\n"
        f"Bal: {user.balance:,} {CURRENCY_NAME} | Streak: {user.daily_streak} days{friendship_text}{daily_xp_text}{daily_quest_msg}{daily_ach_text}"
    )


@router.message(Command("gift", "give", "send"))
async def cmd_gift(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /gift command to send Telecoins to another user."""
    if not message.text:
        return

    args = message.text.split()

    if len(args) < 3:
        await message.answer(
            f"<b>Gift {CURRENCY_NAME}</b>\n\n"
            "Usage: /gift @username [amount]\n"
            "Example: /gift @friend 500\n\n"
            "You can also reply to a user's message:\n"
            "/gift [amount]"
        )
        return

    # Parse amount and recipient
    amount = None
    target_user = None

    # Check if replying to someone
    if message.reply_to_message and message.reply_to_message.from_user:
        target_telegram_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.first_name or "Unknown"
        # Amount is the second arg when replying
        try:
            amount = int(args[1])
        except ValueError:
            await message.answer("Invalid amount! Use a number.\nExample: /gift 500")
            return
    else:
        # Parse @username or user ID
        target_ref = args[1]

        # Try to parse amount
        try:
            amount = int(args[2])
        except ValueError:
            await message.answer("Invalid amount! Use a number.\nExample: /gift @friend 500")
            return

        # Resolve target user
        if target_ref.startswith("@"):
            username = target_ref[1:]
            result = await session.execute(
                select(User).where(User.username == username)
            )
            target_user = result.scalar_one_or_none()
            if not target_user:
                await message.answer(
                    f"User @{username} not found.\n"
                    "They need to use /start first!"
                )
                return
            target_telegram_id = target_user.telegram_id
            target_name = target_user.display_name
        elif target_ref.isdigit():
            target_telegram_id = int(target_ref)
            result = await session.execute(
                select(User).where(User.telegram_id == target_telegram_id)
            )
            target_user = result.scalar_one_or_none()
            if not target_user:
                await message.answer("User not found!")
                return
            target_name = target_user.display_name
        else:
            await message.answer(
                "Invalid user! Use @username or reply to their message.\n"
                "Example: /gift @friend 500"
            )
            return

    # Validate amount
    if amount is None or amount < 1:
        await message.answer(f"Amount must be at least 1 {CURRENCY_SHORT}!")
        return

    if amount > MAX_GIFT_AMOUNT:
        await message.answer(f"Maximum gift amount is {MAX_GIFT_AMOUNT:,} {CURRENCY_SHORT}!")
        return

    # Can't gift yourself
    if not target_user:
        result = await session.execute(
            select(User).where(User.telegram_id == target_telegram_id)
        )
        target_user = result.scalar_one_or_none()
        if not target_user:
            await message.answer("User not found! They need to use /start first.")
            return
        target_name = target_user.display_name

    if target_user.telegram_id == user.telegram_id:
        await message.answer("You can't gift yourself!")
        return

    # Atomic transfer — prevents race conditions where concurrent /gift
    # commands both see sufficient balance and double-spend.
    result = await session.execute(
        update(User)
        .where(User.telegram_id == user.telegram_id)
        .where(User.balance >= amount)
        .values(balance=User.balance - amount)
    )

    if result.rowcount == 0:
        await message.answer(
            f"Not enough {CURRENCY_NAME}!\n"
            f"Your balance: {user.balance:,} {CURRENCY_SHORT}"
        )
        return

    # Credit the recipient atomically
    await session.execute(
        update(User)
        .where(User.telegram_id == target_user.telegram_id)
        .values(balance=User.balance + amount)
    )
    await session.commit()

    # Refresh for accurate display
    await session.refresh(user)

    logger.info(
        "User sent gift",
        from_user=user.telegram_id,
        to_user=target_user.telegram_id,
        amount=amount,
    )

    await message.answer(
        f"✅ Sent <b>{amount:,} {CURRENCY_SHORT}</b> to {target_name}!\n"
        f"Bal: {user.balance:,} {CURRENCY_SHORT}"
    )
