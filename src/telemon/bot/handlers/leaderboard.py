"""Leaderboard handlers for rankings and competitions."""

import math
from enum import Enum

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.config import CURRENCY_SHORT
from telemon.database.models import PokedexEntry, Pokemon, User
from telemon.logging import get_logger

router = Router(name="leaderboard")
logger = get_logger(__name__)

# Constants
ENTRIES_PER_PAGE = 10


class LeaderboardType(str, Enum):
    """Types of leaderboards available."""
    CATCHES = "catches"
    WEALTH = "wealth"
    POKEDEX = "pokedex"
    SHINY = "shiny"
    BATTLES = "battles"
    RATING = "rating"
    GROUP = "group"


# Medal emojis for top 3
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def get_rank_display(rank: int) -> str:
    """Get display string for a rank."""
    if rank in MEDALS:
        return MEDALS[rank]
    return f"#{rank}"


async def get_catches_leaderboard(
    session: AsyncSession, page: int = 1
) -> tuple[list[dict], int]:
    """Get leaderboard by total Pokemon caught."""
    # Count Pokemon per user
    count_query = (
        select(
            Pokemon.owner_id,
            func.count(Pokemon.id).label("total")
        )
        .group_by(Pokemon.owner_id)
        .order_by(func.count(Pokemon.id).desc())
    )
    
    # Get total count
    total_result = await session.execute(
        select(func.count(func.distinct(Pokemon.owner_id)))
    )
    total_users = total_result.scalar() or 0
    
    # Get page
    offset = (page - 1) * ENTRIES_PER_PAGE
    result = await session.execute(
        count_query.offset(offset).limit(ENTRIES_PER_PAGE)
    )
    rows = result.all()
    
    # Batch-fetch user details (fixes N+1 query)
    user_ids = [uid for uid, _ in rows]
    user_map: dict[int, str] = {}
    if user_ids:
        user_result = await session.execute(
            select(User).where(User.telegram_id.in_(user_ids))
        )
        user_map = {
            u.telegram_id: u.display_name for u in user_result.scalars().all()
        }
    
    entries = []
    for i, (user_id, total) in enumerate(rows):
        entries.append({
            "rank": offset + i + 1,
            "user_id": user_id,
            "username": user_map.get(user_id, f"User {user_id}"),
            "value": total,
            "label": "Pokemon",
        })
    
    return entries, total_users


async def get_wealth_leaderboard(
    session: AsyncSession, page: int = 1
) -> tuple[list[dict], int]:
    """Get leaderboard by Telecoin balance."""
    # Get total users with balance > 0
    total_result = await session.execute(
        select(func.count(User.telegram_id)).where(User.balance > 0)
    )
    total_users = total_result.scalar() or 0
    
    # Get page
    offset = (page - 1) * ENTRIES_PER_PAGE
    result = await session.execute(
        select(User)
        .where(User.balance > 0)
        .order_by(User.balance.desc())
        .offset(offset)
        .limit(ENTRIES_PER_PAGE)
    )
    users = result.scalars().all()
    
    entries = []
    for i, user in enumerate(users):
        entries.append({
            "rank": offset + i + 1,
            "user_id": user.telegram_id,
            "username": user.display_name,
            "value": user.balance,
            "label": CURRENCY_SHORT,
        })
    
    return entries, total_users


async def get_pokedex_leaderboard(
    session: AsyncSession, page: int = 1
) -> tuple[list[dict], int]:
    """Get leaderboard by Pokedex completion."""
    # Count caught species per user
    count_query = (
        select(
            PokedexEntry.user_id,
            func.count(PokedexEntry.species_id).label("total")
        )
        .where(PokedexEntry.caught == True)
        .group_by(PokedexEntry.user_id)
        .order_by(func.count(PokedexEntry.species_id).desc())
    )
    
    # Get total users
    total_result = await session.execute(
        select(func.count(func.distinct(PokedexEntry.user_id)))
        .where(PokedexEntry.caught == True)
    )
    total_users = total_result.scalar() or 0
    
    # Get page
    offset = (page - 1) * ENTRIES_PER_PAGE
    result = await session.execute(
        count_query.offset(offset).limit(ENTRIES_PER_PAGE)
    )
    rows = result.all()
    
    # Batch-fetch user details (fixes N+1 query)
    user_ids = [uid for uid, _ in rows]
    user_map: dict[int, str] = {}
    if user_ids:
        user_result = await session.execute(
            select(User).where(User.telegram_id.in_(user_ids))
        )
        user_map = {
            u.telegram_id: u.display_name for u in user_result.scalars().all()
        }
    
    entries = []
    for i, (user_id, total) in enumerate(rows):
        entries.append({
            "rank": offset + i + 1,
            "user_id": user_id,
            "username": user_map.get(user_id, f"User {user_id}"),
            "value": total,
            "label": "species",
        })
    
    return entries, total_users


async def get_shiny_leaderboard(
    session: AsyncSession, page: int = 1
) -> tuple[list[dict], int]:
    """Get leaderboard by shiny Pokemon owned."""
    # Count shiny Pokemon per user
    count_query = (
        select(
            Pokemon.owner_id,
            func.count(Pokemon.id).label("total")
        )
        .where(Pokemon.is_shiny == True)
        .group_by(Pokemon.owner_id)
        .order_by(func.count(Pokemon.id).desc())
    )
    
    # Get total users with shinies
    total_result = await session.execute(
        select(func.count(func.distinct(Pokemon.owner_id)))
        .where(Pokemon.is_shiny == True)
    )
    total_users = total_result.scalar() or 0
    
    # Get page
    offset = (page - 1) * ENTRIES_PER_PAGE
    result = await session.execute(
        count_query.offset(offset).limit(ENTRIES_PER_PAGE)
    )
    rows = result.all()
    
    # Batch-fetch user details (fixes N+1 query)
    user_ids = [uid for uid, _ in rows]
    user_map: dict[int, str] = {}
    if user_ids:
        user_result = await session.execute(
            select(User).where(User.telegram_id.in_(user_ids))
        )
        user_map = {
            u.telegram_id: u.display_name for u in user_result.scalars().all()
        }
    
    entries = []
    for i, (user_id, total) in enumerate(rows):
        entries.append({
            "rank": offset + i + 1,
            "user_id": user_id,
            "username": user_map.get(user_id, f"User {user_id}"),
            "value": total,
            "label": "shinies ✨",
        })
    
    return entries, total_users


async def get_battles_leaderboard(
    session: AsyncSession, page: int = 1
) -> tuple[list[dict], int]:
    """Get leaderboard by battle wins."""
    # Get total users with battles
    total_result = await session.execute(
        select(func.count(User.telegram_id)).where(User.battle_wins > 0)
    )
    total_users = total_result.scalar() or 0
    
    # Get page
    offset = (page - 1) * ENTRIES_PER_PAGE
    result = await session.execute(
        select(User)
        .where(User.battle_wins > 0)
        .order_by(User.battle_wins.desc())
        .offset(offset)
        .limit(ENTRIES_PER_PAGE)
    )
    users = result.scalars().all()
    
    entries = []
    for i, user in enumerate(users):
        win_rate = f"{user.win_rate:.1f}%" if user.total_battles > 0 else "N/A"
        entries.append({
            "rank": offset + i + 1,
            "user_id": user.telegram_id,
            "username": user.display_name,
            "value": user.battle_wins,
            "label": f"wins ({win_rate})",
        })
    
    return entries, total_users


async def get_rating_leaderboard(
    session: AsyncSession, page: int = 1
) -> tuple[list[dict], int]:
    """Get leaderboard by battle rating."""
    # Get total users with battles
    total_result = await session.execute(
        select(func.count(User.telegram_id))
        .where(User.battle_wins + User.battle_losses > 0)
    )
    total_users = total_result.scalar() or 0
    
    # Get page
    offset = (page - 1) * ENTRIES_PER_PAGE
    result = await session.execute(
        select(User)
        .where(User.battle_wins + User.battle_losses > 0)
        .order_by(User.battle_rating.desc())
        .offset(offset)
        .limit(ENTRIES_PER_PAGE)
    )
    users = result.scalars().all()
    
    entries = []
    for i, user in enumerate(users):
        entries.append({
            "rank": offset + i + 1,
            "user_id": user.telegram_id,
            "username": user.display_name,
            "value": user.battle_rating,
            "label": f"rating ({user.battle_wins}W/{user.battle_losses}L)",
        })
    
    return entries, total_users


async def get_group_leaderboard(
    session: AsyncSession, chat_id: int, page: int = 1
) -> tuple[list[dict], int]:
    """Get leaderboard by catches in this specific group."""
    # Count Pokemon caught in this group per user
    count_query = (
        select(
            Pokemon.owner_id,
            func.count(Pokemon.id).label("total")
        )
        .where(Pokemon.caught_in_group_id == chat_id)
        .group_by(Pokemon.owner_id)
        .order_by(func.count(Pokemon.id).desc())
    )
    
    # Get total count of users who caught here
    total_result = await session.execute(
        select(func.count(func.distinct(Pokemon.owner_id)))
        .where(Pokemon.caught_in_group_id == chat_id)
    )
    total_users = total_result.scalar() or 0
    
    # Get page
    offset = (page - 1) * ENTRIES_PER_PAGE
    result = await session.execute(
        count_query.offset(offset).limit(ENTRIES_PER_PAGE)
    )
    rows = result.all()
    
    # Batch-fetch user details (fixes N+1 query)
    user_ids = [uid for uid, _ in rows]
    user_map: dict[int, str] = {}
    if user_ids:
        user_result = await session.execute(
            select(User).where(User.telegram_id.in_(user_ids))
        )
        user_map = {
            u.telegram_id: u.display_name for u in user_result.scalars().all()
        }
    
    entries = []
    for i, (user_id, total) in enumerate(rows):
        entries.append({
            "rank": offset + i + 1,
            "user_id": user_id,
            "username": user_map.get(user_id, f"User {user_id}"),
            "value": total,
            "label": "catches here",
        })
    
    return entries, total_users


async def get_user_rank(
    session: AsyncSession, user_id: int, lb_type: LeaderboardType
) -> int | None:
    """Get a user's rank in a specific leaderboard."""
    if lb_type == LeaderboardType.CATCHES:
        # Count users with more Pokemon
        user_count_result = await session.execute(
            select(func.count(Pokemon.id))
            .where(Pokemon.owner_id == user_id)
        )
        user_count = user_count_result.scalar() or 0
        
        rank_result = await session.execute(
            select(func.count(func.distinct(Pokemon.owner_id)))
            .where(
                select(func.count(Pokemon.id))
                .where(Pokemon.owner_id == Pokemon.owner_id)
                .correlate(Pokemon)
                .scalar_subquery() > user_count
            )
        )
        # Simplified: just get position
        all_counts = await session.execute(
            select(Pokemon.owner_id, func.count(Pokemon.id).label("cnt"))
            .group_by(Pokemon.owner_id)
            .order_by(func.count(Pokemon.id).desc())
        )
        for i, (uid, _) in enumerate(all_counts.all()):
            if uid == user_id:
                return i + 1
        return None
    
    elif lb_type == LeaderboardType.WEALTH:
        user_result = await session.execute(
            select(User.balance).where(User.telegram_id == user_id)
        )
        user_balance = user_result.scalar() or 0
        
        rank_result = await session.execute(
            select(func.count(User.telegram_id))
            .where(User.balance > user_balance)
        )
        return (rank_result.scalar() or 0) + 1
    
    elif lb_type == LeaderboardType.RATING:
        user_result = await session.execute(
            select(User.battle_rating).where(User.telegram_id == user_id)
        )
        user_rating = user_result.scalar() or 1000
        
        rank_result = await session.execute(
            select(func.count(User.telegram_id))
            .where(User.battle_rating > user_rating)
            .where(User.battle_wins + User.battle_losses > 0)
        )
        return (rank_result.scalar() or 0) + 1
    
    return None


def build_leaderboard_keyboard(
    current_type: LeaderboardType, page: int, total_pages: int
) -> InlineKeyboardBuilder:
    """Build keyboard for leaderboard navigation."""
    builder = InlineKeyboardBuilder()
    
    # Category buttons
    categories = [
        ("🎯 Catches", LeaderboardType.CATCHES),
        ("💰 Wealth", LeaderboardType.WEALTH),
        ("📕 Pokédex", LeaderboardType.POKEDEX),
        ("✨ Shiny", LeaderboardType.SHINY),
        ("⚔️ Battles", LeaderboardType.BATTLES),
        ("🏆 Rating", LeaderboardType.RATING),
        ("🏠 Group", LeaderboardType.GROUP),
    ]
    
    for label, lb_type in categories:
        if lb_type == current_type:
            label = f"[{label}]"
        builder.button(text=label, callback_data=f"lb:{lb_type.value}:1")
    
    # Pagination row
    if total_pages > 1:
        if page > 1:
            builder.button(text="◀️", callback_data=f"lb:{current_type.value}:{page - 1}")
        builder.button(text=f"{page}/{total_pages}", callback_data="lb:noop")
        if page < total_pages:
            builder.button(text="▶️", callback_data=f"lb:{current_type.value}:{page + 1}")
    
    # Adjust layout: 4 categories first row, 3 second row, then nav
    builder.adjust(4, 3, 3)
    
    return builder


def format_leaderboard(
    entries: list[dict], lb_type: LeaderboardType, page: int, total: int
) -> str:
    """Format leaderboard entries for display."""
    titles = {
        LeaderboardType.CATCHES: "🎯 Most Pokemon Caught",
        LeaderboardType.WEALTH: "💰 Wealthiest Trainers",
        LeaderboardType.POKEDEX: "📕 Pokédex Completion",
        LeaderboardType.SHINY: "✨ Shiny Collectors",
        LeaderboardType.BATTLES: "⚔️ Battle Champions",
        LeaderboardType.RATING: "🏆 Top Rated",
        LeaderboardType.GROUP: "🏠 Group Catches",
    }
    
    lines = [
        f"<b>{titles.get(lb_type, 'Leaderboard')}</b>",
        f"<i>Showing {len(entries)} of {total} trainers</i>\n",
    ]
    
    for entry in entries:
        rank_display = get_rank_display(entry["rank"])
        value_display = f"{entry['value']:,}" if isinstance(entry["value"], int) else entry["value"]
        lines.append(
            f"{rank_display} <b>{entry['username']}</b> — {value_display} {entry['label']}"
        )
    
    return "\n".join(lines)


@router.message(Command("leaderboard", "lb", "top", "rankings"))
async def cmd_leaderboard(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /leaderboard command."""
    text = message.text or ""
    args = text.split()
    
    # Default to catches leaderboard
    lb_type = LeaderboardType.CATCHES
    page = 1
    
    if len(args) > 1:
        subcommand = args[1].lower()
        
        # Map aliases to types
        type_map = {
            "catches": LeaderboardType.CATCHES,
            "catch": LeaderboardType.CATCHES,
            "pokemon": LeaderboardType.CATCHES,
            "wealth": LeaderboardType.WEALTH,
            "rich": LeaderboardType.WEALTH,
            "money": LeaderboardType.WEALTH,
            "balance": LeaderboardType.WEALTH,
            "tc": LeaderboardType.WEALTH,
            "pokedex": LeaderboardType.POKEDEX,
            "dex": LeaderboardType.POKEDEX,
            "completion": LeaderboardType.POKEDEX,
            "shiny": LeaderboardType.SHINY,
            "shinies": LeaderboardType.SHINY,
            "battles": LeaderboardType.BATTLES,
            "battle": LeaderboardType.BATTLES,
            "wins": LeaderboardType.BATTLES,
            "rating": LeaderboardType.RATING,
            "elo": LeaderboardType.RATING,
            "ranked": LeaderboardType.RATING,
            "group": LeaderboardType.GROUP,
            "server": LeaderboardType.GROUP,
            "chat": LeaderboardType.GROUP,
        }
        
        if subcommand in type_map:
            lb_type = type_map[subcommand]
        elif subcommand == "help":
            await show_leaderboard_help(message)
            return
    
    await show_leaderboard(message, session, user, lb_type, page)


async def show_leaderboard_help(message: Message) -> None:
    """Show leaderboard help."""
    await message.answer(
        "🏆 <b>Leaderboard Commands</b>\n\n"
        "<b>Usage:</b> /leaderboard [category]\n\n"
        "<b>Categories:</b>\n"
        "• catches - Most Pokemon caught\n"
        "• wealth - Highest balance\n"
        "• pokedex - Most species caught\n"
        "• shiny - Most shinies owned\n"
        "• battles - Most battle wins\n"
        "• rating - Highest battle rating\n"
        "• group - Most catches in this group\n\n"
        "<b>Examples:</b>\n"
        "/lb - Default (catches)\n"
        "/lb wealth - Richest trainers\n"
        "/lb shiny - Shiny collectors\n"
        "/top rating - Top rated battlers"
    )


async def show_leaderboard(
    message: Message,
    session: AsyncSession,
    user: User,
    lb_type: LeaderboardType,
    page: int = 1,
) -> None:
    """Show a leaderboard."""
    # Get leaderboard data
    chat_id = message.chat.id if message.chat.type != "private" else None
    
    if lb_type == LeaderboardType.GROUP:
        if not chat_id:
            await message.answer("Group leaderboard is only available in groups!")
            return
        entries, total = await get_group_leaderboard(session, chat_id, page)
    elif lb_type == LeaderboardType.CATCHES:
        entries, total = await get_catches_leaderboard(session, page)
    elif lb_type == LeaderboardType.WEALTH:
        entries, total = await get_wealth_leaderboard(session, page)
    elif lb_type == LeaderboardType.POKEDEX:
        entries, total = await get_pokedex_leaderboard(session, page)
    elif lb_type == LeaderboardType.SHINY:
        entries, total = await get_shiny_leaderboard(session, page)
    elif lb_type == LeaderboardType.BATTLES:
        entries, total = await get_battles_leaderboard(session, page)
    elif lb_type == LeaderboardType.RATING:
        entries, total = await get_rating_leaderboard(session, page)
    else:
        entries, total = [], 0
    
    if not entries:
        await message.answer(
            "🏆 <b>Leaderboard</b>\n\n"
            "No entries yet! Be the first to make it on the board."
        )
        return
    
    total_pages = max(1, math.ceil(total / ENTRIES_PER_PAGE))
    
    # Format leaderboard
    text = format_leaderboard(entries, lb_type, page, total)
    
    # Check if user is on this page
    user_on_page = any(e["user_id"] == user.telegram_id for e in entries)
    
    if not user_on_page:
        # Try to get user's rank
        user_rank = await get_user_rank(session, user.telegram_id, lb_type)
        if user_rank:
            text += f"\n\n<i>Your rank: #{user_rank}</i>"
    
    keyboard = build_leaderboard_keyboard(lb_type, page, total_pages)
    
    await message.answer(text, reply_markup=keyboard.as_markup())


@router.callback_query(F.data.startswith("lb:"))
async def handle_leaderboard_callback(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle leaderboard navigation callbacks."""
    data = callback.data.split(":")
    
    if len(data) < 2:
        await callback.answer("Invalid callback")
        return
    
    action = data[1]
    
    if action == "noop":
        await callback.answer()
        return
    
    # Parse type and page
    try:
        lb_type = LeaderboardType(action)
        page = int(data[2]) if len(data) > 2 else 1
    except (ValueError, IndexError):
        await callback.answer("Invalid leaderboard type")
        return
    
    # Get leaderboard data
    if lb_type == LeaderboardType.GROUP:
        # Use chat_id from the callback message
        chat_id = callback.message.chat.id if callback.message and callback.message.chat.type != "private" else None
        if not chat_id:
            await callback.answer("Group leaderboard unavailable in DMs")
            return
        entries, total = await get_group_leaderboard(session, chat_id, page)
    elif lb_type == LeaderboardType.CATCHES:
        entries, total = await get_catches_leaderboard(session, page)
    elif lb_type == LeaderboardType.WEALTH:
        entries, total = await get_wealth_leaderboard(session, page)
    elif lb_type == LeaderboardType.POKEDEX:
        entries, total = await get_pokedex_leaderboard(session, page)
    elif lb_type == LeaderboardType.SHINY:
        entries, total = await get_shiny_leaderboard(session, page)
    elif lb_type == LeaderboardType.BATTLES:
        entries, total = await get_battles_leaderboard(session, page)
    elif lb_type == LeaderboardType.RATING:
        entries, total = await get_rating_leaderboard(session, page)
    else:
        entries, total = [], 0
    
    if not entries:
        await callback.answer("No entries on this page")
        return
    
    total_pages = max(1, math.ceil(total / ENTRIES_PER_PAGE))
    
    # Format leaderboard
    text = format_leaderboard(entries, lb_type, page, total)
    
    # Check if user is on this page
    user_on_page = any(e["user_id"] == user.telegram_id for e in entries)
    
    if not user_on_page:
        user_rank = await get_user_rank(session, user.telegram_id, lb_type)
        if user_rank:
            text += f"\n\n<i>Your rank: #{user_rank}</i>"
    
    keyboard = build_leaderboard_keyboard(lb_type, page, total_pages)
    
    await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
    await callback.answer()


@router.message(Command("rank", "myrank"))
async def cmd_rank(message: Message, session: AsyncSession, user: User) -> None:
    """Show user's ranks across all leaderboards."""
    lines = [f"📊 <b>{user.display_name}'s Rankings</b>\n"]
    
    # Get Pokemon count
    pokemon_result = await session.execute(
        select(func.count(Pokemon.id)).where(Pokemon.owner_id == user.telegram_id)
    )
    pokemon_count = pokemon_result.scalar() or 0
    catches_rank = await get_user_rank(session, user.telegram_id, LeaderboardType.CATCHES)
    lines.append(f"🎯 <b>Pokemon:</b> {pokemon_count} (Rank #{catches_rank or '?'})")
    
    # Wealth
    wealth_rank = await get_user_rank(session, user.telegram_id, LeaderboardType.WEALTH)
    lines.append(f"💰 <b>Wealth:</b> {user.balance:,} {CURRENCY_SHORT} (Rank #{wealth_rank or '?'})")
    
    # Pokedex
    dex_result = await session.execute(
        select(func.count(PokedexEntry.species_id))
        .where(PokedexEntry.user_id == user.telegram_id)
        .where(PokedexEntry.caught == True)
    )
    dex_count = dex_result.scalar() or 0
    # Simplified rank for pokedex
    lines.append(f"📕 <b>Pokédex:</b> {dex_count}/151 species")
    
    # Shinies
    shiny_result = await session.execute(
        select(func.count(Pokemon.id))
        .where(Pokemon.owner_id == user.telegram_id)
        .where(Pokemon.is_shiny == True)
    )
    shiny_count = shiny_result.scalar() or 0
    lines.append(f"✨ <b>Shinies:</b> {shiny_count}")
    
    # Battles
    if user.total_battles > 0:
        rating_rank = await get_user_rank(session, user.telegram_id, LeaderboardType.RATING)
        lines.append(
            f"⚔️ <b>Battles:</b> {user.battle_wins}W/{user.battle_losses}L "
            f"({user.win_rate:.1f}%)"
        )
        lines.append(f"🏆 <b>Rating:</b> {user.battle_rating} (Rank #{rating_rank or '?'})")
    else:
        lines.append("⚔️ <b>Battles:</b> No battles yet")
    
    lines.append("\n<i>Use /leaderboard to see full rankings!</i>")
    
    await message.answer("\n".join(lines))
