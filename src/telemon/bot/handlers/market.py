"""Market-related handlers for Pokemon marketplace."""

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.config import CURRENCY_SHORT
from telemon.core.constants import VALID_TYPES, MAX_IV_TOTAL, MARKET_MIN_PRICE, MARKET_MAX_PRICE, MARKET_LISTING_DAYS
from telemon.database.models import (
    ListingStatus,
    MarketListing,
    Pokemon,
    PokemonSpecies,
    User,
)
from telemon.logging import get_logger

router = Router(name="market")
logger = get_logger(__name__)

# Constants
LISTINGS_PER_PAGE = 5


class SortOrder(str, Enum):
    """Sort order options for market listings."""
    NEWEST = "newest"
    OLDEST = "oldest"
    PRICE_LOW = "price_low"
    PRICE_HIGH = "price_high"
    LEVEL_LOW = "level_low"
    LEVEL_HIGH = "level_high"
    IV_LOW = "iv_low"
    IV_HIGH = "iv_high"


@dataclass
class MarketFilters:
    """Filters for market search."""
    name: str | None = None
    pokemon_type: str | None = None
    min_level: int | None = None
    max_level: int | None = None
    min_iv: float | None = None
    max_iv: float | None = None
    min_price: int | None = None
    max_price: int | None = None
    shiny_only: bool = False
    sort_by: SortOrder = SortOrder.NEWEST

    def to_query_string(self) -> str:
        """Convert filters to a query string for callback data."""
        parts = []
        if self.name:
            parts.append(f"n={self.name}")
        if self.pokemon_type:
            parts.append(f"t={self.pokemon_type}")
        if self.min_level:
            parts.append(f"lmin={self.min_level}")
        if self.max_level:
            parts.append(f"lmax={self.max_level}")
        if self.min_iv:
            parts.append(f"ivmin={self.min_iv}")
        if self.max_iv:
            parts.append(f"ivmax={self.max_iv}")
        if self.min_price:
            parts.append(f"pmin={self.min_price}")
        if self.max_price:
            parts.append(f"pmax={self.max_price}")
        if self.shiny_only:
            parts.append("shiny=1")
        if self.sort_by != SortOrder.NEWEST:
            parts.append(f"sort={self.sort_by.value}")
        return "|".join(parts) if parts else ""

    @classmethod
    def from_query_string(cls, query: str) -> "MarketFilters":
        """Parse filters from a query string."""
        filters = cls()
        if not query:
            return filters

        for part in query.split("|"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key == "n":
                filters.name = value
            elif key == "t":
                filters.pokemon_type = value
            elif key == "lmin":
                filters.min_level = int(value)
            elif key == "lmax":
                filters.max_level = int(value)
            elif key == "ivmin":
                filters.min_iv = float(value)
            elif key == "ivmax":
                filters.max_iv = float(value)
            elif key == "pmin":
                filters.min_price = int(value)
            elif key == "pmax":
                filters.max_price = int(value)
            elif key == "shiny":
                filters.shiny_only = value == "1"
            elif key == "sort":
                try:
                    filters.sort_by = SortOrder(value)
                except ValueError:
                    pass
        return filters

    def describe(self) -> str:
        """Get a human-readable description of active filters."""
        parts = []
        if self.name:
            parts.append(f"Name: {self.name}")
        if self.pokemon_type:
            parts.append(f"Type: {self.pokemon_type.title()}")
        if self.min_level or self.max_level:
            if self.min_level and self.max_level:
                parts.append(f"Level: {self.min_level}-{self.max_level}")
            elif self.min_level:
                parts.append(f"Level: {self.min_level}+")
            else:
                parts.append(f"Level: 1-{self.max_level}")
        if self.min_iv or self.max_iv:
            if self.min_iv and self.max_iv:
                parts.append(f"IV: {self.min_iv:.0f}%-{self.max_iv:.0f}%")
            elif self.min_iv:
                parts.append(f"IV: {self.min_iv:.0f}%+")
            else:
                parts.append(f"IV: 0-{self.max_iv:.0f}%")
        if self.min_price or self.max_price:
            if self.min_price and self.max_price:
                parts.append(f"Price: {self.min_price:,}-{self.max_price:,} {CURRENCY_SHORT}")
            elif self.min_price:
                parts.append(f"Price: {self.min_price:,}+ {CURRENCY_SHORT}")
            else:
                parts.append(f"Price: 0-{self.max_price:,} {CURRENCY_SHORT}")
        if self.shiny_only:
            parts.append("Shiny Only")
        if self.sort_by != SortOrder.NEWEST:
            sort_names = {
                SortOrder.OLDEST: "Oldest First",
                SortOrder.PRICE_LOW: "Price: Low→High",
                SortOrder.PRICE_HIGH: "Price: High→Low",
                SortOrder.LEVEL_LOW: "Level: Low→High",
                SortOrder.LEVEL_HIGH: "Level: High→Low",
                SortOrder.IV_LOW: "IV: Low→High",
                SortOrder.IV_HIGH: "IV: High→Low",
            }
            parts.append(f"Sort: {sort_names.get(self.sort_by, self.sort_by.value)}")
        return " | ".join(parts) if parts else "No filters"


def parse_filters_from_args(args: list[str]) -> MarketFilters:
    """Parse filter arguments from command args."""
    filters = MarketFilters()
    text = " ".join(args)

    # Parse --name or -n
    name_match = re.search(r'(?:--name|-n)\s+(\S+)', text)
    if name_match:
        filters.name = name_match.group(1)

    # Parse --type or -t
    type_match = re.search(r'(?:--type|-t)\s+(\S+)', text)
    if type_match:
        type_val = type_match.group(1).lower()
        if type_val in VALID_TYPES:
            filters.pokemon_type = type_val

    # Parse --level or -l (supports ranges like 50-100, 50+, or just 50)
    level_match = re.search(r'(?:--level|-l)\s+(\d+)(?:-(\d+)|\+)?', text)
    if level_match:
        filters.min_level = int(level_match.group(1))
        if level_match.group(2):
            filters.max_level = int(level_match.group(2))
        elif not text[level_match.end()-1:level_match.end()] == '+':
            # Just a single number means exact level
            filters.max_level = filters.min_level

    # Parse --iv or -i (supports ranges like 90-100, 90+, or just 90)
    iv_match = re.search(r'(?:--iv|-i)\s+(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?)|\+)?', text)
    if iv_match:
        filters.min_iv = float(iv_match.group(1))
        if iv_match.group(2):
            filters.max_iv = float(iv_match.group(2))

    # Parse --price or -p (supports ranges and k/m suffixes)
    price_match = re.search(r'(?:--price|-p)\s+(\d+[km]?)(?:-(\d+[km]?))?', text, re.IGNORECASE)
    if price_match:
        def parse_price(s: str) -> int:
            s = s.lower().replace(",", "")
            if s.endswith("k"):
                return int(float(s[:-1]) * 1000)
            if s.endswith("m"):
                return int(float(s[:-1]) * 1000000)
            return int(s)

        filters.min_price = parse_price(price_match.group(1))
        if price_match.group(2):
            filters.max_price = parse_price(price_match.group(2))

    # Parse --shiny or -s
    if re.search(r'(?:--shiny|-s)(?:\s|$)', text):
        filters.shiny_only = True

    # Parse --sort (newest, oldest, price, level, iv with optional +/- for direction)
    sort_match = re.search(r'--sort\s+(\S+)', text)
    if sort_match:
        sort_val = sort_match.group(1).lower()
        sort_map = {
            "newest": SortOrder.NEWEST,
            "oldest": SortOrder.OLDEST,
            "price": SortOrder.PRICE_LOW,
            "price+": SortOrder.PRICE_HIGH,
            "price-": SortOrder.PRICE_LOW,
            "level": SortOrder.LEVEL_HIGH,
            "level+": SortOrder.LEVEL_HIGH,
            "level-": SortOrder.LEVEL_LOW,
            "iv": SortOrder.IV_HIGH,
            "iv+": SortOrder.IV_HIGH,
            "iv-": SortOrder.IV_LOW,
        }
        if sort_val in sort_map:
            filters.sort_by = sort_map[sort_val]

    # If no flags found, treat entire args as a name search
    if not any([filters.name, filters.pokemon_type, filters.min_level,
                filters.min_iv, filters.min_price, filters.shiny_only,
                filters.sort_by != SortOrder.NEWEST]):
        # Check if args look like filter flags
        if args and not args[0].startswith("-"):
            filters.name = " ".join(args)

    return filters


async def get_user_pokemon_list(session: AsyncSession, user_id: int) -> list[Pokemon]:
    """Get all Pokemon for a user ordered by catch time."""
    result = await session.execute(
        select(Pokemon)
        .where(Pokemon.owner_id == user_id)
        .order_by(Pokemon.caught_at.asc())
    )
    return list(result.scalars().all())


async def get_active_listings(
    session: AsyncSession,
    page: int = 1,
    filters: MarketFilters | None = None,
) -> tuple[list[MarketListing], int]:
    """Get active market listings with pagination and filters."""
    if filters is None:
        filters = MarketFilters()

    # We need to join with Pokemon to apply filters and sorting
    # Build the base query with joins
    base_conditions = [
        MarketListing.status == ListingStatus.ACTIVE,
        MarketListing.expires_at > datetime.utcnow(),
    ]

    # Build Pokemon filter conditions
    pokemon_conditions = []

    if filters.name:
        pokemon_conditions.append(PokemonSpecies.name.ilike(f"%{filters.name}%"))

    if filters.pokemon_type:
        # Match either type1 or type2
        pokemon_conditions.append(
            (PokemonSpecies.type1.ilike(filters.pokemon_type)) |
            (PokemonSpecies.type2.ilike(filters.pokemon_type))
        )

    if filters.min_level:
        pokemon_conditions.append(Pokemon.level >= filters.min_level)

    if filters.max_level:
        pokemon_conditions.append(Pokemon.level <= filters.max_level)

    if filters.shiny_only:
        pokemon_conditions.append(Pokemon.is_shiny == True)

    if filters.min_price:
        base_conditions.append(MarketListing.price >= filters.min_price)

    if filters.max_price:
        base_conditions.append(MarketListing.price <= filters.max_price)

    # Build query with joins for Pokemon-based filters
    if pokemon_conditions or filters.min_iv or filters.max_iv or filters.sort_by in [
        SortOrder.LEVEL_LOW, SortOrder.LEVEL_HIGH, SortOrder.IV_LOW, SortOrder.IV_HIGH
    ]:
        # We need to join with Pokemon (and Species for type/name filters)
        query = (
            select(MarketListing)
            .join(Pokemon, MarketListing.pokemon_id == Pokemon.id)
            .join(PokemonSpecies, Pokemon.species_id == PokemonSpecies.national_dex)
            .where(and_(*base_conditions))
        )

        if pokemon_conditions:
            query = query.where(and_(*pokemon_conditions))

        # IV filtering requires computing total IV
        if filters.min_iv or filters.max_iv:
            iv_total = (
                Pokemon.iv_hp + Pokemon.iv_attack + Pokemon.iv_defense +
                Pokemon.iv_sp_attack + Pokemon.iv_sp_defense + Pokemon.iv_speed
            )
            iv_percentage = (iv_total * 100.0 / MAX_IV_TOTAL)

            if filters.min_iv:
                query = query.where(iv_percentage >= filters.min_iv)
            if filters.max_iv:
                query = query.where(iv_percentage <= filters.max_iv)

        # Count query
        count_query = (
            select(func.count(MarketListing.id))
            .join(Pokemon, MarketListing.pokemon_id == Pokemon.id)
            .join(PokemonSpecies, Pokemon.species_id == PokemonSpecies.national_dex)
            .where(and_(*base_conditions))
        )
        if pokemon_conditions:
            count_query = count_query.where(and_(*pokemon_conditions))
        if filters.min_iv or filters.max_iv:
            iv_total = (
                Pokemon.iv_hp + Pokemon.iv_attack + Pokemon.iv_defense +
                Pokemon.iv_sp_attack + Pokemon.iv_sp_defense + Pokemon.iv_speed
            )
            iv_percentage = (iv_total * 100.0 / MAX_IV_TOTAL)
            if filters.min_iv:
                count_query = count_query.where(iv_percentage >= filters.min_iv)
            if filters.max_iv:
                count_query = count_query.where(iv_percentage <= filters.max_iv)

        # Apply sorting
        if filters.sort_by == SortOrder.NEWEST:
            query = query.order_by(MarketListing.listed_at.desc())
        elif filters.sort_by == SortOrder.OLDEST:
            query = query.order_by(MarketListing.listed_at.asc())
        elif filters.sort_by == SortOrder.PRICE_LOW:
            query = query.order_by(MarketListing.price.asc())
        elif filters.sort_by == SortOrder.PRICE_HIGH:
            query = query.order_by(MarketListing.price.desc())
        elif filters.sort_by == SortOrder.LEVEL_LOW:
            query = query.order_by(Pokemon.level.asc())
        elif filters.sort_by == SortOrder.LEVEL_HIGH:
            query = query.order_by(Pokemon.level.desc())
        elif filters.sort_by == SortOrder.IV_LOW:
            iv_total = (
                Pokemon.iv_hp + Pokemon.iv_attack + Pokemon.iv_defense +
                Pokemon.iv_sp_attack + Pokemon.iv_sp_defense + Pokemon.iv_speed
            )
            query = query.order_by(iv_total.asc())
        elif filters.sort_by == SortOrder.IV_HIGH:
            iv_total = (
                Pokemon.iv_hp + Pokemon.iv_attack + Pokemon.iv_defense +
                Pokemon.iv_sp_attack + Pokemon.iv_sp_defense + Pokemon.iv_speed
            )
            query = query.order_by(iv_total.desc())

    else:
        # Simple query without Pokemon joins
        query = select(MarketListing).where(and_(*base_conditions))
        count_query = select(func.count(MarketListing.id)).where(and_(*base_conditions))

        # Apply sorting
        if filters.sort_by == SortOrder.NEWEST:
            query = query.order_by(MarketListing.listed_at.desc())
        elif filters.sort_by == SortOrder.OLDEST:
            query = query.order_by(MarketListing.listed_at.asc())
        elif filters.sort_by == SortOrder.PRICE_LOW:
            query = query.order_by(MarketListing.price.asc())
        elif filters.sort_by == SortOrder.PRICE_HIGH:
            query = query.order_by(MarketListing.price.desc())
        else:
            query = query.order_by(MarketListing.listed_at.desc())

    # Get total count
    count_result = await session.execute(count_query)
    total_count = count_result.scalar() or 0

    # Apply pagination
    offset = (page - 1) * LISTINGS_PER_PAGE
    query = query.offset(offset).limit(LISTINGS_PER_PAGE)

    result = await session.execute(query)
    listings = list(result.scalars().all())

    return listings, total_count


async def get_user_listings(
    session: AsyncSession, user_id: int
) -> list[MarketListing]:
    """Get active listings for a user."""
    result = await session.execute(
        select(MarketListing)
        .where(MarketListing.seller_id == user_id)
        .where(MarketListing.status == ListingStatus.ACTIVE)
        .order_by(MarketListing.listed_at.desc())
    )
    return list(result.scalars().all())


async def format_listing(
    session: AsyncSession, listing: MarketListing, index: int | None = None
) -> str:
    """Format a single listing for display."""
    # Get Pokemon details
    result = await session.execute(
        select(Pokemon).where(Pokemon.id == listing.pokemon_id)
    )
    pokemon = result.scalar_one_or_none()

    if not pokemon:
        return f"#{index or '?'} - [Pokemon not found]"

    # Get seller info
    seller_result = await session.execute(
        select(User).where(User.telegram_id == listing.seller_id)
    )
    seller = seller_result.scalar_one_or_none()
    seller_name = seller.display_name if seller else f"User {listing.seller_id}"

    shiny = " ✨" if pokemon.is_shiny else ""
    iv_pct = pokemon.iv_percentage

    # Pokemon type
    types = pokemon.species.type1
    if pokemon.species.type2:
        types += f"/{pokemon.species.type2}"

    # Time remaining
    time_left = listing.expires_at - datetime.utcnow()
    if time_left.days > 0:
        time_str = f"{time_left.days}d"
    elif time_left.seconds > 3600:
        time_str = f"{time_left.seconds // 3600}h"
    else:
        time_str = f"{time_left.seconds // 60}m"

    prefix = f"<b>#{index}</b> " if index else ""

    return (
        f"{prefix}<b>{pokemon.species.name}</b>{shiny} Lv.{pokemon.level} [{types}]\n"
        f"   IV: {iv_pct:.1f}% | Price: {listing.price:,} {CURRENCY_SHORT}\n"
        f"   Seller: {seller_name} | Expires: {time_str}"
    )


def build_market_keyboard(
    page: int, total_pages: int, filters: MarketFilters | None = None
) -> InlineKeyboardBuilder:
    """Build pagination keyboard for market."""
    builder = InlineKeyboardBuilder()
    filter_str = filters.to_query_string() if filters else ""

    if total_pages > 1:
        buttons = []

        if page > 1:
            buttons.append(("◀️ Prev", f"market:page:{page - 1}:{filter_str}"))

        buttons.append((f"{page}/{total_pages}", "market:noop"))

        if page < total_pages:
            buttons.append(("Next ▶️", f"market:page:{page + 1}:{filter_str}"))

        for text, callback_data in buttons:
            builder.button(text=text, callback_data=callback_data)

        builder.adjust(3)

    return builder


@router.message(Command("market"))
async def cmd_market(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /market command and subcommands."""
    text = message.text or ""
    args = text.split()

    if len(args) < 2:
        # Show market browse
        await show_market(message, session, page=1)
        return

    subcommand = args[1].lower()

    if subcommand == "sell":
        await market_sell(message, session, user, args[2:])
    elif subcommand == "buy":
        await market_buy(message, session, user, args[2:])
    elif subcommand == "cancel":
        await market_cancel(message, session, user, args[2:])
    elif subcommand == "listings" or subcommand == "my":
        await market_my_listings(message, session, user)
    elif subcommand == "search" or subcommand == "filter" or subcommand == "find":
        await market_search(message, session, args[2:])
    elif subcommand == "info":
        await market_info(message, session, args[2:])
    elif subcommand == "help":
        await market_help(message)
    else:
        # Maybe it's a page number or a filter
        if subcommand.isdigit():
            await show_market(message, session, page=int(subcommand))
        elif subcommand.startswith("-"):
            # It's a filter, parse from args[1:]
            await market_search(message, session, args[1:])
        else:
            await message.answer(
                "❓ Unknown market command.\n\n"
                "Use /market help for full command list."
            )


async def market_help(message: Message) -> None:
    """Show market help with all commands and filters."""
    await message.answer(
        "🏪 <b>Market Commands</b>\n\n"
        "<b>Browsing:</b>\n"
        "/market - Browse all listings\n"
        "/market [page] - Go to page\n"
        "/market info [#] - View listing details\n\n"
        "<b>Trading:</b>\n"
        "/market sell [id] [price] - List Pokemon\n"
        "/market buy [#] - Purchase listing\n"
        "/market cancel [#] - Cancel your listing\n"
        "/market listings - Your active listings\n\n"
        "<b>Searching:</b>\n"
        "/market search [filters]\n\n"
        "<b>Available Filters:</b>\n"
        "<code>--name, -n</code> Pokemon name\n"
        "<code>--type, -t</code> Pokemon type\n"
        "<code>--level, -l</code> Level (50, 50+, 50-100)\n"
        "<code>--iv, -i</code> IV % (90, 90+, 80-100)\n"
        "<code>--price, -p</code> Price (10k, 1m, 10k-100k)\n"
        "<code>--shiny, -s</code> Shiny only\n"
        "<code>--sort</code> Sort order\n\n"
        "<b>Sort Options:</b>\n"
        "newest, oldest, price, price+, level, level+, iv, iv+\n\n"
        "<b>Examples:</b>\n"
        "<code>/market search pikachu</code>\n"
        "<code>/market search -t fire -l 50+</code>\n"
        "<code>/market search -i 90+ --shiny</code>\n"
        "<code>/market search -p 10k-100k --sort iv+</code>"
    )


async def show_market(
    message: Message,
    session: AsyncSession,
    page: int = 1,
    filters: MarketFilters | None = None,
) -> None:
    """Show market listings with pagination."""
    listings, total_count = await get_active_listings(
        session, page=page, filters=filters
    )

    if not listings and page == 1:
        filter_note = ""
        if filters and filters.describe() != "No filters":
            filter_note = f"\n\n🔍 Filters: {filters.describe()}"
        await message.answer(
            f"🏪 <b>Global Marketplace</b>\n\n"
            f"No listings found.{filter_note}\n\n"
            f"<i>Be the first to list a Pokemon!</i>\n"
            f"Use: /market sell [pokemon_id] [price]"
        )
        return

    total_pages = math.ceil(total_count / LISTINGS_PER_PAGE)

    # Format listings
    lines = ["🏪 <b>Global Marketplace</b>\n"]

    if filters and filters.describe() != "No filters":
        lines.append(f"🔍 {filters.describe()}\n")

    lines.append(f"<i>Showing {len(listings)} of {total_count} listings</i>\n")

    # Number listings globally based on page
    start_index = (page - 1) * LISTINGS_PER_PAGE + 1
    for i, listing in enumerate(listings):
        listing_text = await format_listing(session, listing, index=start_index + i)
        lines.append(f"\n{listing_text}")

    lines.append("\n\n<b>Commands:</b>")
    lines.append("/market buy [#] - Purchase")
    lines.append("/market info [#] - Details")
    lines.append("/market help - All commands")

    keyboard = build_market_keyboard(page, total_pages, filters)

    await message.answer("\n".join(lines), reply_markup=keyboard.as_markup())


async def market_sell(
    message: Message, session: AsyncSession, user: User, args: list
) -> None:
    """List a Pokemon for sale on the market."""
    if len(args) < 2:
        await message.answer(
            "📝 <b>Sell Pokemon</b>\n\n"
            "Usage: /market sell [pokemon_id] [price]\n\n"
            "Example: /market sell 5 10000\n"
            f"(Lists your 5th Pokemon for 10,000 {CURRENCY_SHORT})\n\n"
            "Price shortcuts: 10k = 10,000 | 1m = 1,000,000\n\n"
            f"Min price: {MARKET_MIN_PRICE:,} {CURRENCY_SHORT}\n"
            f"Max price: {MARKET_MAX_PRICE:,} {CURRENCY_SHORT}\n"
            f"Listings expire after {MARKET_LISTING_DAYS} days"
        )
        return

    # Parse Pokemon ID (supports l/latest/0 for latest/selected)
    LATEST_ALIASES = {"l", "-l", "--latest", "-latest", "latest"}
    pokemon_arg = args[0].lower()
    pokemon = None

    if pokemon_arg in LATEST_ALIASES:
        # Get most recently caught Pokemon
        result = await session.execute(
            select(Pokemon)
            .where(Pokemon.owner_id == user.telegram_id)
            .order_by(Pokemon.caught_at.desc())
            .limit(1)
        )
        pokemon = result.scalar_one_or_none()
        if not pokemon:
            await message.answer("❌ You don't have any Pokemon!")
            return
    elif pokemon_arg == "0":
        # Use selected Pokemon
        if not user.selected_pokemon_id:
            await message.answer("❌ No Pokemon selected! Use /select first.")
            return
        result = await session.execute(
            select(Pokemon)
            .where(Pokemon.id == user.selected_pokemon_id)
            .where(Pokemon.owner_id == user.telegram_id)
        )
        pokemon = result.scalar_one_or_none()
        if not pokemon:
            await message.answer("❌ Selected Pokemon not found.")
            return
    else:
        try:
            pokemon_idx = int(args[0])
        except ValueError:
            await message.answer("❌ Invalid Pokemon ID. Use a number, `l` for latest, or `0` for selected.")
            return

        # Get user's Pokemon by index
        pokemon_list = await get_user_pokemon_list(session, user.telegram_id)

        if not pokemon_list:
            await message.answer("❌ You don't have any Pokemon!")
            return

        if pokemon_idx < 1 or pokemon_idx > len(pokemon_list):
            await message.answer(
                f"❌ Invalid Pokemon ID! You have {len(pokemon_list)} Pokemon.\n"
                "Use /pokemon to see your collection."
            )
            return

        pokemon = pokemon_list[pokemon_idx - 1]

    # Parse price
    try:
        price_str = args[1].lower().replace(",", "")
        if price_str.endswith("k"):
            price = int(float(price_str[:-1]) * 1000)
        elif price_str.endswith("m"):
            price = int(float(price_str[:-1]) * 1000000)
        else:
            price = int(price_str)
    except ValueError:
        await message.answer("❌ Invalid price. Use a number (supports k/m suffix).")
        return

    if price < MARKET_MIN_PRICE:
        await message.answer(f"❌ Minimum price is {MARKET_MIN_PRICE:,} {CURRENCY_SHORT}.")
        return

    if price > MARKET_MAX_PRICE:
        await message.answer(f"❌ Maximum price is {MARKET_MAX_PRICE:,} {CURRENCY_SHORT}.")
        return

    # Check if Pokemon can be listed
    if pokemon.is_on_market:
        await message.answer(f"❌ {pokemon.species.name} is already on the market!")
        return

    if pokemon.is_in_trade:
        await message.answer(f"❌ {pokemon.species.name} is in an active trade!")
        return

    if pokemon.is_favorite:
        await message.answer(
            f"❌ {pokemon.species.name} is favorited!\n"
            "Unfavorite it first to sell."
        )
        return

    # Check if user's selected Pokemon
    if str(pokemon.id) == user.selected_pokemon_id:
        await message.answer(
            f"❌ {pokemon.species.name} is your selected Pokemon!\n"
            "Select a different Pokemon first with /select"
        )
        return

    # Create listing
    expires_at = datetime.utcnow() + timedelta(days=MARKET_LISTING_DAYS)

    listing = MarketListing(
        seller_id=user.telegram_id,
        pokemon_id=pokemon.id,
        price=price,
        status=ListingStatus.ACTIVE,
        expires_at=expires_at,
    )
    session.add(listing)

    # Mark Pokemon as on market
    pokemon.is_on_market = True

    await session.commit()

    logger.info(
        "Market listing created",
        listing_id=str(listing.id),
        seller_id=user.telegram_id,
        pokemon_id=str(pokemon.id),
        pokemon_name=pokemon.species.name,
        price=price,
    )

    shiny = " ✨" if pokemon.is_shiny else ""
    await message.answer(
        f"✅ <b>Listed on Market!</b>\n\n"
        f"Pokemon: <b>{pokemon.species.name}</b>{shiny} Lv.{pokemon.level}\n"
        f"IV: {pokemon.iv_percentage:.1f}%\n"
        f"Price: {price:,} {CURRENCY_SHORT}\n"
        f"Expires: {MARKET_LISTING_DAYS} days\n\n"
        f"<i>Your Pokemon is now visible to all trainers!</i>"
    )


async def market_buy(
    message: Message, session: AsyncSession, user: User, args: list
) -> None:
    """Buy a Pokemon from the market."""
    if not args:
        await message.answer(
            "🛒 <b>Buy Pokemon</b>\n\n"
            "Usage: /market buy [listing_#]\n\n"
            "Example: /market buy 3\n"
            "(Buys listing #3 from the market)\n\n"
            "Use /market to browse listings first."
        )
        return

    # Parse listing number
    try:
        listing_num = int(args[0])
    except ValueError:
        await message.answer("❌ Invalid listing number.")
        return

    if listing_num < 1:
        await message.answer("❌ Listing number must be positive.")
        return

    # Get the listing by index
    offset = listing_num - 1
    result = await session.execute(
        select(MarketListing)
        .where(MarketListing.status == ListingStatus.ACTIVE)
        .where(MarketListing.expires_at > datetime.utcnow())
        .order_by(MarketListing.listed_at.desc())
        .offset(offset)
        .limit(1)
    )
    listing = result.scalar_one_or_none()

    if not listing:
        await message.answer(
            f"❌ Listing #{listing_num} not found.\n"
            "Use /market to see available listings."
        )
        return

    # Check if user is buying their own listing
    if listing.seller_id == user.telegram_id:
        await message.answer("❌ You can't buy your own listing!")
        return

    # Check user balance
    if user.balance < listing.price:
        await message.answer(
            f"❌ Not enough Telecoins!\n\n"
            f"Price: {listing.price:,} {CURRENCY_SHORT}\n"
            f"Your balance: {user.balance:,} {CURRENCY_SHORT}\n"
            f"Need: {listing.price - user.balance:,} {CURRENCY_SHORT} more"
        )
        return

    # Get Pokemon
    result = await session.execute(
        select(Pokemon).where(Pokemon.id == listing.pokemon_id)
    )
    pokemon = result.scalar_one_or_none()

    if not pokemon:
        await message.answer("❌ Pokemon not found. Listing may be corrupted.")
        return

    # Get seller
    result = await session.execute(
        select(User).where(User.telegram_id == listing.seller_id)
    )
    seller = result.scalar_one_or_none()

    if not seller:
        await message.answer("❌ Seller not found.")
        return

    # Atomic purchase — debit buyer, credit seller
    debit_result = await session.execute(
        update(User)
        .where(User.telegram_id == user.telegram_id)
        .where(User.balance >= listing.price)
        .values(balance=User.balance - listing.price)
    )

    if debit_result.rowcount == 0:
        await message.answer(
            f"❌ Not enough {CURRENCY_SHORT}! Your balance may have changed."
        )
        return

    await session.execute(
        update(User)
        .where(User.telegram_id == seller.telegram_id)
        .values(balance=User.balance + listing.price)
    )

    # Transfer Pokemon
    pokemon.owner_id = user.telegram_id
    pokemon.is_on_market = False

    # Update listing
    listing.status = ListingStatus.SOLD
    listing.buyer_id = user.telegram_id
    listing.sold_at = datetime.utcnow()

    await session.commit()

    # Refresh for accurate display
    await session.refresh(user)

    logger.info(
        "Market purchase completed",
        listing_id=str(listing.id),
        buyer_id=user.telegram_id,
        seller_id=seller.telegram_id,
        pokemon_id=str(pokemon.id),
        pokemon_name=pokemon.species.name,
        price=listing.price,
    )

    shiny = " ✨" if pokemon.is_shiny else ""
    await message.answer(
        f"🎉 <b>Purchase Complete!</b>\n\n"
        f"You bought <b>{pokemon.species.name}</b>{shiny} Lv.{pokemon.level}\n"
        f"IV: {pokemon.iv_percentage:.1f}%\n"
        f"Price: {listing.price:,} {CURRENCY_SHORT}\n\n"
        f"Seller: {seller.display_name}\n"
        f"Your new balance: {user.balance:,} {CURRENCY_SHORT}\n\n"
        f"<i>Use /pokemon to see your new Pokemon!</i>"
    )

    # DM notify seller about the sale
    try:
        from telemon.core.notifications import notify_market_sale
        await notify_market_sale(
            bot=message.bot,
            seller_id=seller.telegram_id,
            pokemon_name=pokemon.species.name,
            price=listing.price,
            buyer_name=user.display_name,
        )
    except Exception as e:
        logger.warning("Market sale DM notification failed", error=str(e), seller_id=seller.telegram_id)


async def market_cancel(
    message: Message, session: AsyncSession, user: User, args: list
) -> None:
    """Cancel a user's market listing."""
    if not args:
        await message.answer(
            "❌ <b>Cancel Listing</b>\n\n"
            "Usage: /market cancel [listing_#]\n\n"
            "Use /market listings to see your active listings."
        )
        return

    # Parse listing number (from user's listings)
    try:
        listing_num = int(args[0])
    except ValueError:
        await message.answer("❌ Invalid listing number.")
        return

    # Get user's listings
    user_listings = await get_user_listings(session, user.telegram_id)

    if not user_listings:
        await message.answer("❌ You don't have any active listings.")
        return

    if listing_num < 1 or listing_num > len(user_listings):
        await message.answer(
            f"❌ Invalid listing number. You have {len(user_listings)} active listings.\n"
            "Use /market listings to see them."
        )
        return

    listing = user_listings[listing_num - 1]

    # Get Pokemon
    result = await session.execute(
        select(Pokemon).where(Pokemon.id == listing.pokemon_id)
    )
    pokemon = result.scalar_one_or_none()

    if not pokemon:
        await message.answer("❌ Pokemon not found.")
        return

    # Cancel listing
    listing.status = ListingStatus.CANCELLED
    pokemon.is_on_market = False

    await session.commit()

    logger.info(
        "Market listing cancelled",
        listing_id=str(listing.id),
        seller_id=user.telegram_id,
        pokemon_id=str(pokemon.id),
    )

    await message.answer(
        f"✅ <b>Listing Cancelled</b>\n\n"
        f"<b>{pokemon.species.name}</b> has been removed from the market.\n"
        f"It's back in your collection!"
    )


async def market_my_listings(
    message: Message, session: AsyncSession, user: User
) -> None:
    """Show user's active listings."""
    listings = await get_user_listings(session, user.telegram_id)

    if not listings:
        await message.answer(
            "📋 <b>Your Listings</b>\n\n"
            "You don't have any active listings.\n\n"
            "Use /market sell [pokemon_id] [price] to list a Pokemon!"
        )
        return

    lines = [
        "📋 <b>Your Active Listings</b>\n",
        f"<i>You have {len(listings)} active listing(s)</i>\n",
    ]

    for i, listing in enumerate(listings, 1):
        # Get Pokemon
        result = await session.execute(
            select(Pokemon).where(Pokemon.id == listing.pokemon_id)
        )
        pokemon = result.scalar_one_or_none()

        if not pokemon:
            continue

        shiny = " ✨" if pokemon.is_shiny else ""
        time_left = listing.expires_at - datetime.utcnow()
        if time_left.days > 0:
            time_str = f"{time_left.days}d remaining"
        elif time_left.seconds > 3600:
            time_str = f"{time_left.seconds // 3600}h remaining"
        else:
            time_str = f"{time_left.seconds // 60}m remaining"

        lines.append(
            f"\n<b>#{i}</b> {pokemon.species.name}{shiny} Lv.{pokemon.level}\n"
            f"   IV: {pokemon.iv_percentage:.1f}% | Price: {listing.price:,} {CURRENCY_SHORT}\n"
            f"   Views: {listing.view_count} | {time_str}"
        )

    lines.append("\n\n<b>Commands:</b>")
    lines.append("/market cancel [#] - Remove listing")

    await message.answer("\n".join(lines))


async def market_search(
    message: Message, session: AsyncSession, args: list
) -> None:
    """Search market with filters."""
    if not args:
        await message.answer(
            "🔍 <b>Search Market</b>\n\n"
            "Usage: /market search [filters]\n\n"
            "<b>Examples:</b>\n"
            "<code>/market search pikachu</code>\n"
            "<code>/market search --type fire</code>\n"
            "<code>/market search -l 50+ -i 90+</code>\n"
            "<code>/market search -t water -p 10k-50k</code>\n"
            "<code>/market search --shiny --sort iv+</code>\n\n"
            "Use /market help for all filter options."
        )
        return

    filters = parse_filters_from_args(args)
    await show_market(message, session, page=1, filters=filters)


async def market_info(
    message: Message, session: AsyncSession, args: list
) -> None:
    """Show detailed info about a listing."""
    if not args:
        await message.answer(
            "ℹ️ <b>Listing Info</b>\n\n"
            "Usage: /market info [listing_#]\n\n"
            "Example: /market info 3\n"
            "(Shows details for listing #3)"
        )
        return

    # Parse listing number
    try:
        listing_num = int(args[0])
    except ValueError:
        await message.answer("❌ Invalid listing number.")
        return

    # Get listing
    offset = listing_num - 1
    result = await session.execute(
        select(MarketListing)
        .where(MarketListing.status == ListingStatus.ACTIVE)
        .where(MarketListing.expires_at > datetime.utcnow())
        .order_by(MarketListing.listed_at.desc())
        .offset(offset)
        .limit(1)
    )
    listing = result.scalar_one_or_none()

    if not listing:
        await message.answer(f"❌ Listing #{listing_num} not found.")
        return

    # Increment view count
    listing.view_count += 1
    await session.commit()

    # Get Pokemon details
    result = await session.execute(
        select(Pokemon).where(Pokemon.id == listing.pokemon_id)
    )
    pokemon = result.scalar_one_or_none()

    if not pokemon:
        await message.answer("❌ Pokemon not found.")
        return

    # Get seller
    result = await session.execute(
        select(User).where(User.telegram_id == listing.seller_id)
    )
    seller = result.scalar_one_or_none()

    shiny = " ✨" if pokemon.is_shiny else ""
    time_left = listing.expires_at - datetime.utcnow()
    if time_left.days > 0:
        time_str = f"{time_left.days} days"
    elif time_left.seconds > 3600:
        time_str = f"{time_left.seconds // 3600} hours"
    else:
        time_str = f"{time_left.seconds // 60} minutes"

    # Pokemon types
    types = pokemon.species.type1
    if pokemon.species.type2:
        types += f" / {pokemon.species.type2}"

    # Format IVs
    iv_line = (
        f"HP: {pokemon.iv_hp} | ATK: {pokemon.iv_attack} | DEF: {pokemon.iv_defense}\n"
        f"SpA: {pokemon.iv_sp_attack} | SpD: {pokemon.iv_sp_defense} | SPE: {pokemon.iv_speed}"
    )

    await message.answer(
        f"ℹ️ <b>Listing #{listing_num} Details</b>\n\n"
        f"<b>{pokemon.species.name}</b>{shiny}\n"
        f"Type: {types.title()}\n"
        f"Level: {pokemon.level}\n"
        f"Nature: {pokemon.nature.title()}\n"
        f"Gender: {pokemon.gender or 'Unknown'}\n\n"
        f"<b>IVs ({pokemon.iv_percentage:.1f}%)</b>\n"
        f"{iv_line}\n\n"
        f"<b>Price:</b> {listing.price:,} {CURRENCY_SHORT}\n"
        f"<b>Seller:</b> {seller.display_name if seller else 'Unknown'}\n"
        f"<b>Views:</b> {listing.view_count}\n"
        f"<b>Expires in:</b> {time_str}\n\n"
        f"<i>Use /market buy {listing_num} to purchase</i>"
    )


@router.callback_query(F.data.startswith("market:"))
async def handle_market_callback(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    """Handle market pagination callbacks."""
    data = callback.data.split(":")

    if len(data) < 2:
        await callback.answer("Invalid callback")
        return

    action = data[1]

    if action == "noop":
        await callback.answer()
        return

    if action == "page":
        page = int(data[2]) if len(data) > 2 else 1
        filter_str = data[3] if len(data) > 3 and data[3] else ""
        filters = MarketFilters.from_query_string(filter_str)

        listings, total_count = await get_active_listings(
            session, page=page, filters=filters
        )

        if not listings:
            await callback.answer("No listings on this page")
            return

        total_pages = math.ceil(total_count / LISTINGS_PER_PAGE)

        # Format listings
        lines = ["🏪 <b>Global Marketplace</b>\n"]

        if filters and filters.describe() != "No filters":
            lines.append(f"🔍 {filters.describe()}\n")

        lines.append(f"<i>Showing {len(listings)} of {total_count} listings</i>\n")

        start_index = (page - 1) * LISTINGS_PER_PAGE + 1
        for i, listing in enumerate(listings):
            listing_text = await format_listing(session, listing, index=start_index + i)
            lines.append(f"\n{listing_text}")

        lines.append("\n\n<b>Commands:</b>")
        lines.append("/market buy [#] - Purchase")
        lines.append("/market info [#] - Details")

        keyboard = build_market_keyboard(page, total_pages, filters)

        await callback.message.edit_text(
            "\n".join(lines), reply_markup=keyboard.as_markup()
        )
        await callback.answer()


async def cleanup_expired_listings(session: AsyncSession) -> int:
    """Mark expired listings as expired and return Pokemon. Returns count."""
    # Find expired active listings
    result = await session.execute(
        select(MarketListing)
        .where(MarketListing.status == ListingStatus.ACTIVE)
        .where(MarketListing.expires_at <= datetime.utcnow())
    )
    expired = list(result.scalars().all())

    count = 0
    for listing in expired:
        listing.status = ListingStatus.EXPIRED

        # Return Pokemon to owner
        pokemon_result = await session.execute(
            select(Pokemon).where(Pokemon.id == listing.pokemon_id)
        )
        pokemon = pokemon_result.scalar_one_or_none()
        if pokemon:
            pokemon.is_on_market = False
            count += 1

    if count > 0:
        await session.commit()
        logger.info("Cleaned up expired listings", count=count)

    return count
