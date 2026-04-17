"""Trade-related handlers for Pokemon trading between users."""

import uuid
from datetime import datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.config import CURRENCY_NAME, CURRENCY_SHORT
from telemon.core.evolution import check_evolution, evolve_pokemon
from telemon.database.models import Pokemon, PokemonSpecies, User
from telemon.database.models.trade import Trade, TradeHistory, TradeStatus
from telemon.logging import get_logger

router = Router(name="trade")
logger = get_logger(__name__)


async def get_active_trade(session: AsyncSession, user_id: int) -> Trade | None:
    """Get active trade for a user (includes waiting-for-accept trades)."""
    result = await session.execute(
        select(Trade)
        .where(
            or_(Trade.user1_id == user_id, Trade.user2_id == user_id)
        )
        .where(Trade.status.in_([
            TradeStatus.WAITING_ACCEPT,
            TradeStatus.PENDING,
            TradeStatus.CONFIRMED_ONE,
        ]))
    )
    return result.scalar_one_or_none()


async def get_pending_invite(session: AsyncSession, user_id: int) -> Trade | None:
    """Get a trade invite waiting for this user to accept."""
    result = await session.execute(
        select(Trade)
        .where(Trade.user2_id == user_id)
        .where(Trade.status == TradeStatus.WAITING_ACCEPT)
    )
    return result.scalar_one_or_none()


async def get_user_pokemon_list(session: AsyncSession, user_id: int) -> list[Pokemon]:
    """Get all Pokemon for a user."""
    result = await session.execute(
        select(Pokemon)
        .where(Pokemon.owner_id == user_id)
        .order_by(Pokemon.caught_at.asc())
    )
    return list(result.scalars().all())


async def format_trade_status(session: AsyncSession, trade: Trade) -> str:
    """Format trade status message."""
    # Get user info
    user1_result = await session.execute(
        select(User).where(User.telegram_id == trade.user1_id)
    )
    user1 = user1_result.scalar_one_or_none()

    user2_result = await session.execute(
        select(User).where(User.telegram_id == trade.user2_id)
    )
    user2 = user2_result.scalar_one_or_none()

    user1_name = user1.username or f"User {trade.user1_id}" if user1 else f"User {trade.user1_id}"
    user2_name = user2.username or f"User {trade.user2_id}" if user2 else f"User {trade.user2_id}"

    # Get Pokemon details
    user1_pokemon = []
    for poke_id in trade.user1_pokemon_ids or []:
        result = await session.execute(
            select(Pokemon).where(Pokemon.id == poke_id)
        )
        poke = result.scalar_one_or_none()
        if poke:
            shiny = " " if poke.is_shiny else ""
            user1_pokemon.append(f"  {shiny}{poke.species.name} Lv.{poke.level}")

    user2_pokemon = []
    for poke_id in trade.user2_pokemon_ids or []:
        result = await session.execute(
            select(Pokemon).where(Pokemon.id == poke_id)
        )
        poke = result.scalar_one_or_none()
        if poke:
            shiny = " " if poke.is_shiny else ""
            user2_pokemon.append(f"  {shiny}{poke.species.name} Lv.{poke.level}")

    # Build status message
    u1_confirm = "" if trade.user1_confirmed else ""
    u2_confirm = "" if trade.user2_confirmed else ""

    lines = [
        "<b>Trade Session</b>\n",
        f"<b>{user1_name}</b> {u1_confirm}",
    ]

    if user1_pokemon:
        lines.extend(user1_pokemon)
    else:
        lines.append("  <i>No Pokemon</i>")

    if trade.user1_coins > 0:
        lines.append(f"  + {trade.user1_coins:,} {CURRENCY_SHORT}")

    lines.append("")
    lines.append(f"<b>{user2_name}</b> {u2_confirm}")

    if user2_pokemon:
        lines.extend(user2_pokemon)
    else:
        lines.append("  <i>No Pokemon</i>")

    if trade.user2_coins > 0:
        lines.append(f"  + {trade.user2_coins:,} {CURRENCY_SHORT}")

    lines.append("")

    if trade.both_confirmed:
        lines.append(" <b>Both confirmed! Completing trade...</b>")
    else:
        lines.append("<i>Use /trade confirm to accept</i>")
        lines.append("<i>Use /trade cancel to abort</i>")

    return "\n".join(lines)


@router.message(Command("trade"))
async def cmd_trade(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /trade command and subcommands."""
    text = message.text or ""
    args = text.split()

    if len(args) < 2:
        # Show help
        await message.answer(
            "<b>Trading System</b>\n\n"
            "<b>Commands:</b>\n"
            "/trade @username - Send trade request\n"
            "/trade accept - Accept incoming trade request\n"
            "/trade reject - Reject incoming trade request\n"
            "/trade add <id> - Add Pokemon to trade\n"
            "/trade remove <id> - Remove Pokemon from trade\n"
            f"/trade coins [amount] - Add {CURRENCY_NAME}\n"
            "/trade confirm - Confirm the trade\n"
            "/trade cancel - Cancel the trade\n"
            "/trade status - View current trade\n\n"
            "<i>Both parties must /trade confirm to complete</i>\n"
            "<i>⏳ Trades auto-cancel after 5 min of inactivity</i>"
        )
        return

    subcommand = args[1].lower()

    # Check for @username to start a trade
    if subcommand.startswith("@"):
        await start_trade(message, session, user, subcommand[1:])
        return

    # Handle subcommands
    if subcommand == "accept":
        await trade_accept(message, session, user)
    elif subcommand == "reject":
        await trade_reject(message, session, user)
    elif subcommand == "add":
        await trade_add_pokemon(message, session, user, args[2:])
    elif subcommand == "remove":
        await trade_remove_pokemon(message, session, user, args[2:])
    elif subcommand == "coins":
        await trade_add_coins(message, session, user, args[2:])
    elif subcommand == "confirm":
        await trade_confirm(message, session, user)
    elif subcommand == "cancel":
        await trade_cancel(message, session, user)
    elif subcommand == "status":
        await trade_status(message, session, user)
    else:
        # Maybe it's a user ID
        if subcommand.isdigit():
            await start_trade_by_id(message, session, user, int(subcommand))
        else:
            await message.answer(
                "❌ Unknown trade command. Use /trade for help."
            )


async def start_trade(
    message: Message, session: AsyncSession, user: User, target_username: str
) -> None:
    """Start a trade with another user by username."""
    # Check if user already has an active trade
    active_trade = await get_active_trade(session, user.telegram_id)
    if active_trade:
        await message.answer(
            " You already have an active trade!\n"
            "Use /trade cancel to cancel it first."
        )
        return

    # Find target user
    result = await session.execute(
        select(User).where(User.username.ilike(target_username))
    )
    target_user = result.scalar_one_or_none()

    if not target_user:
        await message.answer(
            f" User @{target_username} not found.\n"
            "They need to /start the bot first."
        )
        return

    if target_user.telegram_id == user.telegram_id:
        await message.answer(" You can't trade with yourself!")
        return

    # Check if target has an active trade
    target_trade = await get_active_trade(session, target_user.telegram_id)
    if target_trade:
        await message.answer(
            f" @{target_username} is already in a trade session."
        )
        return

    # Create new trade in WAITING_ACCEPT state
    trade = Trade(
        user1_id=user.telegram_id,
        user2_id=target_user.telegram_id,
        user1_pokemon_ids=[],
        user2_pokemon_ids=[],
        chat_id=message.chat.id,
        status=TradeStatus.WAITING_ACCEPT,
    )
    session.add(trade)
    await session.commit()

    logger.info(
        "Trade invite sent",
        user1_id=user.telegram_id,
        user2_id=target_user.telegram_id,
        trade_id=str(trade.id),
    )

    # Build accept/reject keyboard
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Accept", callback_data=f"trade:accept:{trade.id}")
    builder.button(text="❌ Reject", callback_data=f"trade:reject:{trade.id}")
    builder.adjust(2)

    await message.answer(
        f"📨 <b>Trade Request Sent!</b>\n\n"
        f"@{user.username or 'You'} wants to trade with @{target_username}\n\n"
        f"@{target_username} — use the buttons below or /trade accept / /trade reject\n"
        "<i>⏳ This request expires in 5 minutes</i>",
        reply_markup=builder.as_markup(),
    )


async def start_trade_by_id(
    message: Message, session: AsyncSession, user: User, target_id: int
) -> None:
    """Start a trade with another user by Telegram ID."""
    # Check if user already has an active trade
    active_trade = await get_active_trade(session, user.telegram_id)
    if active_trade:
        await message.answer(
            " You already have an active trade!\n"
            "Use /trade cancel to cancel it first."
        )
        return

    # Find target user
    result = await session.execute(
        select(User).where(User.telegram_id == target_id)
    )
    target_user = result.scalar_one_or_none()

    if not target_user:
        await message.answer(
            f" User {target_id} not found.\n"
            "They need to /start the bot first."
        )
        return

    if target_user.telegram_id == user.telegram_id:
        await message.answer(" You can't trade with yourself!")
        return

    # Check if target has an active trade
    target_trade = await get_active_trade(session, target_user.telegram_id)
    if target_trade:
        await message.answer(
            f" User {target_id} is already in a trade session."
        )
        return

    # Create new trade in WAITING_ACCEPT state
    trade = Trade(
        user1_id=user.telegram_id,
        user2_id=target_user.telegram_id,
        user1_pokemon_ids=[],
        user2_pokemon_ids=[],
        chat_id=message.chat.id,
        status=TradeStatus.WAITING_ACCEPT,
    )
    session.add(trade)
    await session.commit()

    target_name = target_user.username or f"User {target_id}"

    logger.info(
        "Trade invite sent",
        user1_id=user.telegram_id,
        user2_id=target_user.telegram_id,
        trade_id=str(trade.id),
    )

    # Build accept/reject keyboard
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Accept", callback_data=f"trade:accept:{trade.id}")
    builder.button(text="❌ Reject", callback_data=f"trade:reject:{trade.id}")
    builder.adjust(2)

    await message.answer(
        f"📨 <b>Trade Request Sent!</b>\n\n"
        f"You want to trade with @{target_name}\n\n"
        f"@{target_name} — use the buttons below or /trade accept / /trade reject\n"
        "<i>⏳ This request expires in 5 minutes</i>",
        reply_markup=builder.as_markup(),
    )


async def trade_add_pokemon(
    message: Message, session: AsyncSession, user: User, args: list
) -> None:
    """Add a Pokemon to the trade."""
    if not args:
        await message.answer(" Usage: /trade add [pokemon_id]")
        return

    # Get active trade
    trade = await get_active_trade(session, user.telegram_id)
    if not trade:
        await message.answer("❌ You don't have an active trade!")
        return

    # Block actions while waiting for accept
    if trade.status == TradeStatus.WAITING_ACCEPT:
        await message.answer("⏳ Waiting for the other player to accept the trade request.")
        return

    # Get user's Pokemon list
    pokemon_list = await get_user_pokemon_list(session, user.telegram_id)
    
    if not pokemon_list:
        await message.answer(" You don't have any Pokemon!")
        return

    # Parse Pokemon index
    try:
        pokemon_idx = int(args[0])
    except ValueError:
        await message.answer(" Please provide a valid Pokemon ID number.")
        return

    if pokemon_idx < 1 or pokemon_idx > len(pokemon_list):
        await message.answer(f" Invalid Pokemon ID! You have {len(pokemon_list)} Pokemon.")
        return

    poke = pokemon_list[pokemon_idx - 1]

    # Check if Pokemon is favorited
    if poke.is_favorite:
        await message.answer(f"❌ {poke.species.name} is favorited! Unfavorite it first.")
        return

    # Check if Pokemon is the user's selected Pokemon
    if str(poke.id) == user.selected_pokemon_id:
        await message.answer(f"❌ {poke.species.name} is your selected Pokemon! Select a different one first.")
        return

    # Check if Pokemon is tradeable
    if not poke.is_tradeable:
        await message.answer(
            f" {poke.species.name} cannot be traded right now.\n"
            "(May be on market or already in a trade)"
        )
        return

    # Determine which user in the trade
    if trade.user1_id == user.telegram_id:
        pokemon_ids = list(trade.user1_pokemon_ids or [])
        if poke.id in pokemon_ids:
            await message.answer(f" {poke.species.name} is already in the trade!")
            return
        pokemon_ids.append(poke.id)
        trade.user1_pokemon_ids = pokemon_ids
        # Reset confirmations when trade changes
        trade.user1_confirmed = False
        trade.user2_confirmed = False
    else:
        pokemon_ids = list(trade.user2_pokemon_ids or [])
        if poke.id in pokemon_ids:
            await message.answer(f" {poke.species.name} is already in the trade!")
            return
        pokemon_ids.append(poke.id)
        trade.user2_pokemon_ids = pokemon_ids
        # Reset confirmations when trade changes
        trade.user1_confirmed = False
        trade.user2_confirmed = False

    # Mark Pokemon as in trade and update activity
    poke.is_in_trade = True
    trade.last_activity_at = datetime.utcnow()
    await session.commit()

    shiny = " " if poke.is_shiny else ""
    await message.answer(
        f" Added {shiny}<b>{poke.species.name}</b> Lv.{poke.level} to trade!\n\n"
        + await format_trade_status(session, trade)
    )


async def trade_remove_pokemon(
    message: Message, session: AsyncSession, user: User, args: list
) -> None:
    """Remove a Pokemon from the trade."""
    if not args:
        await message.answer(" Usage: /trade remove [pokemon_id]")
        return

    # Get active trade
    trade = await get_active_trade(session, user.telegram_id)
    if not trade:
        await message.answer("❌ You don't have an active trade!")
        return

    # Block actions while waiting for accept
    if trade.status == TradeStatus.WAITING_ACCEPT:
        await message.answer("⏳ Waiting for the other player to accept the trade request.")
        return

    # Get user's Pokemon list
    pokemon_list = await get_user_pokemon_list(session, user.telegram_id)

    # Parse Pokemon index
    try:
        pokemon_idx = int(args[0])
    except ValueError:
        await message.answer(" Please provide a valid Pokemon ID number.")
        return

    if pokemon_idx < 1 or pokemon_idx > len(pokemon_list):
        await message.answer(f" Invalid Pokemon ID!")
        return

    poke = pokemon_list[pokemon_idx - 1]

    # Determine which user in the trade
    if trade.user1_id == user.telegram_id:
        pokemon_ids = list(trade.user1_pokemon_ids or [])
        if poke.id not in pokemon_ids:
            await message.answer(f" {poke.species.name} is not in the trade!")
            return
        pokemon_ids.remove(poke.id)
        trade.user1_pokemon_ids = pokemon_ids
        trade.user1_confirmed = False
        trade.user2_confirmed = False
    else:
        pokemon_ids = list(trade.user2_pokemon_ids or [])
        if poke.id not in pokemon_ids:
            await message.answer(f" {poke.species.name} is not in the trade!")
            return
        pokemon_ids.remove(poke.id)
        trade.user2_pokemon_ids = pokemon_ids
        trade.user1_confirmed = False
        trade.user2_confirmed = False

    # Unmark Pokemon and update activity
    poke.is_in_trade = False
    trade.last_activity_at = datetime.utcnow()
    await session.commit()

    await message.answer(
        f" Removed <b>{poke.species.name}</b> from trade!\n\n"
        + await format_trade_status(session, trade)
    )


async def trade_add_coins(
    message: Message, session: AsyncSession, user: User, args: list
) -> None:
    """Add coins to the trade."""
    if not args:
        await message.answer(" Usage: /trade coins [amount]")
        return

    # Get active trade
    trade = await get_active_trade(session, user.telegram_id)
    if not trade:
        await message.answer("❌ You don't have an active trade!")
        return

    # Block actions while waiting for accept
    if trade.status == TradeStatus.WAITING_ACCEPT:
        await message.answer("⏳ Waiting for the other player to accept the trade request.")
        return

    # Parse amount
    try:
        amount = int(args[0])
    except ValueError:
        await message.answer(" Please provide a valid number.")
        return

    if amount < 0:
        await message.answer(" Amount must be positive!")
        return

    if amount > user.balance:
        await message.answer(
            f" You only have {user.balance:,} {CURRENCY_SHORT}!\n"
            f"Requested: {amount:,} {CURRENCY_SHORT}"
        )
        return

    # Add coins to trade
    if trade.user1_id == user.telegram_id:
        trade.user1_coins = amount
        trade.user1_confirmed = False
        trade.user2_confirmed = False
    else:
        trade.user2_coins = amount
        trade.user1_confirmed = False
        trade.user2_confirmed = False

    trade.last_activity_at = datetime.utcnow()
    await session.commit()

    await message.answer(
        f" Set trade offer to {amount:,} {CURRENCY_SHORT}!\n\n"
        + await format_trade_status(session, trade)
    )


async def trade_accept(message: Message, session: AsyncSession, user: User) -> None:
    """Accept an incoming trade request."""
    invite = await get_pending_invite(session, user.telegram_id)
    if not invite:
        await message.answer("❌ You don't have any pending trade requests!")
        return

    # Transition to active PENDING state
    invite.status = TradeStatus.PENDING
    invite.last_activity_at = datetime.utcnow()
    await session.commit()

    # Get initiator name
    user1_result = await session.execute(
        select(User).where(User.telegram_id == invite.user1_id)
    )
    user1 = user1_result.scalar_one_or_none()
    user1_name = user1.username or f"User {invite.user1_id}" if user1 else f"User {invite.user1_id}"

    logger.info(
        "Trade accepted",
        user1_id=invite.user1_id,
        user2_id=user.telegram_id,
        trade_id=str(invite.id),
    )

    await message.answer(
        f"✅ <b>Trade Accepted!</b>\n\n"
        f"@{user.username or 'You'} accepted the trade with @{user1_name}\n\n"
        "Use /trade add [pokemon_id] to add Pokemon\n"
        f"Use /trade coins [amount] to add {CURRENCY_NAME}\n"
        "Use /trade confirm when ready\n"
        "Use /trade cancel to abort"
    )


async def trade_reject(message: Message, session: AsyncSession, user: User) -> None:
    """Reject an incoming trade request."""
    invite = await get_pending_invite(session, user.telegram_id)
    if not invite:
        await message.answer("❌ You don't have any pending trade requests!")
        return

    invite.status = TradeStatus.CANCELLED
    await session.commit()

    # Get initiator name
    user1_result = await session.execute(
        select(User).where(User.telegram_id == invite.user1_id)
    )
    user1 = user1_result.scalar_one_or_none()
    user1_name = user1.username or f"User {invite.user1_id}" if user1 else f"User {invite.user1_id}"

    logger.info(
        "Trade rejected",
        user1_id=invite.user1_id,
        user2_id=user.telegram_id,
        trade_id=str(invite.id),
    )

    await message.answer(
        f"❌ Trade request from @{user1_name} has been rejected."
    )


async def trade_confirm(message: Message, session: AsyncSession, user: User) -> None:
    """Confirm the trade."""
    trade = await get_active_trade(session, user.telegram_id)
    if not trade:
        await message.answer("❌ You don't have an active trade!")
        return

    # Block confirm while waiting for accept
    if trade.status == TradeStatus.WAITING_ACCEPT:
        await message.answer("⏳ Waiting for the other player to accept the trade request.")
        return

    # Set confirmation
    if trade.user1_id == user.telegram_id:
        trade.user1_confirmed = True
    else:
        trade.user2_confirmed = True

    trade.last_activity_at = datetime.utcnow()

    # Check if both confirmed
    if trade.both_confirmed:
        # Execute the trade!
        await execute_trade(message, session, trade)
    else:
        await session.commit()
        await message.answer(
            " You've confirmed the trade!\n"
            "Waiting for the other party...\n\n"
            + await format_trade_status(session, trade)
        )


async def execute_trade(message: Message, session: AsyncSession, trade: Trade) -> None:
    """Execute a confirmed trade."""
    # Get both users
    user1_result = await session.execute(
        select(User).where(User.telegram_id == trade.user1_id)
    )
    user1 = user1_result.scalar_one()

    user2_result = await session.execute(
        select(User).where(User.telegram_id == trade.user2_id)
    )
    user2 = user2_result.scalar_one()

    # Verify coin balances
    if trade.user1_coins > user1.balance:
        trade.user1_confirmed = False
        await session.commit()
        await message.answer(
            f" Trade failed: User 1 doesn't have enough {CURRENCY_SHORT}!"
        )
        return

    if trade.user2_coins > user2.balance:
        trade.user2_confirmed = False
        await session.commit()
        await message.answer(
            f" Trade failed: User 2 doesn't have enough {CURRENCY_SHORT}!"
        )
        return

    # Transfer Pokemon from user1 to user2
    traded_pokemon = []
    for poke_id in trade.user1_pokemon_ids or []:
        result = await session.execute(
            select(Pokemon).where(Pokemon.id == poke_id)
        )
        poke = result.scalar_one_or_none()
        if poke:
            # Clear selected_pokemon_id if trading away selected Pokemon
            if user1.selected_pokemon_id and str(poke.id) == user1.selected_pokemon_id:
                user1.selected_pokemon_id = None
            poke.owner_id = trade.user2_id
            poke.is_in_trade = False
            traded_pokemon.append((poke, trade.user2_id))

    # Transfer Pokemon from user2 to user1
    for poke_id in trade.user2_pokemon_ids or []:
        result = await session.execute(
            select(Pokemon).where(Pokemon.id == poke_id)
        )
        poke = result.scalar_one_or_none()
        if poke:
            # Clear selected_pokemon_id if trading away selected Pokemon
            if user2.selected_pokemon_id and str(poke.id) == user2.selected_pokemon_id:
                user2.selected_pokemon_id = None
            poke.owner_id = trade.user1_id
            poke.is_in_trade = False
            traded_pokemon.append((poke, trade.user1_id))

    # Transfer coins
    user1.balance -= trade.user1_coins
    user1.balance += trade.user2_coins
    user2.balance -= trade.user2_coins
    user2.balance += trade.user1_coins

    # Mark trade as completed
    trade.status = TradeStatus.COMPLETED
    trade.completed_at = datetime.utcnow()

    # Increment trade counters
    user1.total_trades += 1
    user2.total_trades += 1

    # Create history record
    history = TradeHistory(
        trade_id=trade.id,
        user1_id=trade.user1_id,
        user2_id=trade.user2_id,
        user1_pokemon_count=len(trade.user1_pokemon_ids or []),
        user2_pokemon_count=len(trade.user2_pokemon_ids or []),
        user1_coins=trade.user1_coins,
        user2_coins=trade.user2_coins,
    )
    session.add(history)

    await session.commit()

    logger.info(
        "Trade completed",
        trade_id=str(trade.id),
        user1_id=trade.user1_id,
        user2_id=trade.user2_id,
        user1_pokemon_count=len(trade.user1_pokemon_ids or []),
        user2_pokemon_count=len(trade.user2_pokemon_ids or []),
    )

    try:
        # Check for trade evolutions
        evolution_messages = []
        for poke, new_owner_id in traded_pokemon:
            # Refresh to ensure species data is available for evolution checks
            await session.refresh(poke, attribute_names=['species'])
            
            from telemon.core.evolution import check_evolution, evolve_pokemon
            evo_result = await check_evolution(
                session, poke, new_owner_id, is_trade=True
            )
            if evo_result.can_evolve:
                success, evo_msg = await evolve_pokemon(
                    session, poke, new_owner_id, is_trade=True
                )
                if success:
                    await session.refresh(poke)
                    evolution_messages.append(
                        f" {evo_msg}"
                    )
                    logger.info(
                        "Trade evolution occurred",
                        pokemon_id=str(poke.id),
                        new_species=poke.species.name,
                        new_owner_id=new_owner_id,
                    )

        await session.commit()

        # Build completion message
        user1_name = user1.username or f"User {trade.user1_id}"
        user2_name = user2.username or f"User {trade.user2_id}"

        response = (
            f" <b>Trade Complete!</b>\n\n"
            f"@{user1_name}  @{user2_name}\n\n"
        )

        if trade.user1_pokemon_ids:
            response += f"@{user1_name} sent {len(trade.user1_pokemon_ids)} Pokemon\n"
        if trade.user2_pokemon_ids:
            response += f"@{user2_name} sent {len(trade.user2_pokemon_ids)} Pokemon\n"
        if trade.user1_coins:
            response += f"@{user1_name} sent {trade.user1_coins:,} {CURRENCY_SHORT}\n"
        if trade.user2_coins:
            response += f"@{user2_name} sent {trade.user2_coins:,} {CURRENCY_SHORT}\n"

        if evolution_messages:
            response += "\n" + "\n".join(evolution_messages)

        # Quest progress for trade (was missing for direct trades)
        from telemon.core.quests import update_quest_progress
        await update_quest_progress(session, trade.user1_id, "trade")
        await update_quest_progress(session, trade.user2_id, "trade")

        # XP rewards from trading
        from telemon.core.leveling import calculate_trade_xp, add_xp_to_pokemon, format_xp_message
        trade_xp = calculate_trade_xp()

        for trader_id in [trade.user1_id, trade.user2_id]:
            trader_result = await session.execute(
                select(User).where(User.telegram_id == trader_id)
            )
            trader = trader_result.scalar_one_or_none()
            if trader and trader.selected_pokemon_id:
                xp_added, levels_gained, learned_moves = await add_xp_to_pokemon(
                    session, trader.selected_pokemon_id, trade_xp
                )
                if xp_added > 0 and levels_gained:
                    poke_r = await session.execute(
                        select(Pokemon).where(Pokemon.id == trader.selected_pokemon_id)
                    )
                    poke = poke_r.scalar_one_or_none()
                    if poke:
                        response += f"\n{poke.display_name} leveled up to Lv.{poke.level}!"
        await session.commit()

        # Achievement hooks for trade
        from telemon.core.achievements import check_achievements, format_achievement_notification
        trade_achs_1 = await check_achievements(session, trade.user1_id, "trade")
        trade_achs_2 = await check_achievements(session, trade.user2_id, "trade")
        all_trade_achs = trade_achs_1 + trade_achs_2
        if all_trade_achs:
            await session.commit()
            response += format_achievement_notification(all_trade_achs)

    except Exception as e:
        logger.error("Error in post-trade processing", error=str(e), exc_info=True)
        # Fallback response if not built yet or if error occurred
        if 'response' not in locals():
            user1_name = user1.username or f"User {trade.user1_id}"
            user2_name = user2.username or f"User {trade.user2_id}"
            response = (
                f" <b>Trade Complete!</b>\n\n"
                f"@{user1_name}  @{user2_name}\n\n"
                "Trade data saved successfully.\n"
                "⚠️ Some post-trade updates (Evolutions/XP/Quests) may have failed."
            )
        else:
             response += "\n⚠️ Some updates (Evolutions/XP/Quests) may have failed."

    await message.answer(response)


async def trade_cancel(message: Message, session: AsyncSession, user: User) -> None:
    """Cancel the active trade."""
    trade = await get_active_trade(session, user.telegram_id)
    if not trade:
        await message.answer(" You don't have an active trade!")
        return

    # Unmark all Pokemon
    for poke_id in (trade.user1_pokemon_ids or []) + (trade.user2_pokemon_ids or []):
        result = await session.execute(
            select(Pokemon).where(Pokemon.id == poke_id)
        )
        poke = result.scalar_one_or_none()
        if poke:
            poke.is_in_trade = False

    # Mark trade as cancelled
    trade.status = TradeStatus.CANCELLED
    await session.commit()

    logger.info(
        "Trade cancelled",
        trade_id=str(trade.id),
        cancelled_by=user.telegram_id,
    )

    await message.answer(" Trade cancelled. All Pokemon returned.")


async def trade_status(message: Message, session: AsyncSession, user: User) -> None:
    """Show current trade status."""
    trade = await get_active_trade(session, user.telegram_id)
    if not trade:
        await message.answer(" You don't have an active trade.")
        return

    await message.answer(await format_trade_status(session, trade))


# ---- Callback query handlers for inline Accept/Reject buttons ----


@router.callback_query(lambda c: c.data and c.data.startswith("trade:accept:"))
async def callback_trade_accept(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle trade accept via inline button."""
    if not callback.data:
        return

    trade_id = callback.data.split(":")[2]

    result = await session.execute(
        select(Trade).where(Trade.id == trade_id)
    )
    trade = result.scalar_one_or_none()

    if not trade:
        await callback.answer("Trade not found!", show_alert=True)
        return

    if trade.user2_id != user.telegram_id:
        await callback.answer("This trade request is not for you!", show_alert=True)
        return

    if trade.status != TradeStatus.WAITING_ACCEPT:
        await callback.answer("This trade request is no longer available!", show_alert=True)
        return

    # Transition to active PENDING state
    trade.status = TradeStatus.PENDING
    trade.last_activity_at = datetime.utcnow()
    await session.commit()

    # Get initiator name
    user1_result = await session.execute(
        select(User).where(User.telegram_id == trade.user1_id)
    )
    user1 = user1_result.scalar_one_or_none()
    user1_name = user1.username or f"User {trade.user1_id}" if user1 else f"User {trade.user1_id}"

    logger.info(
        "Trade accepted via button",
        user1_id=trade.user1_id,
        user2_id=user.telegram_id,
        trade_id=str(trade.id),
    )

    await callback.message.edit_text(
        f"✅ <b>Trade Accepted!</b>\n\n"
        f"@{user.username or 'You'} accepted the trade with @{user1_name}\n\n"
        "Use /trade add [pokemon_id] to add Pokemon\n"
        f"Use /trade coins [amount] to add {CURRENCY_NAME}\n"
        "Use /trade confirm when ready\n"
        "Use /trade cancel to abort"
    )
    await callback.answer("Trade accepted!")


@router.callback_query(lambda c: c.data and c.data.startswith("trade:reject:"))
async def callback_trade_reject(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle trade reject via inline button."""
    if not callback.data:
        return

    trade_id = callback.data.split(":")[2]

    result = await session.execute(
        select(Trade).where(Trade.id == trade_id)
    )
    trade = result.scalar_one_or_none()

    if not trade:
        await callback.answer("Trade not found!", show_alert=True)
        return

    if trade.user2_id != user.telegram_id:
        await callback.answer("This trade request is not for you!", show_alert=True)
        return

    if trade.status != TradeStatus.WAITING_ACCEPT:
        await callback.answer("This trade request is no longer available!", show_alert=True)
        return

    trade.status = TradeStatus.CANCELLED
    await session.commit()

    # Get initiator name
    user1_result = await session.execute(
        select(User).where(User.telegram_id == trade.user1_id)
    )
    user1 = user1_result.scalar_one_or_none()
    user1_name = user1.username or f"User {trade.user1_id}" if user1 else f"User {trade.user1_id}"

    logger.info(
        "Trade rejected via button",
        user1_id=trade.user1_id,
        user2_id=user.telegram_id,
        trade_id=str(trade.id),
    )

    await callback.message.edit_text(
        f"❌ Trade request from @{user1_name} has been rejected."
    )
    await callback.answer("Trade rejected!")
