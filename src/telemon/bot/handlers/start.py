"""Start and welcome handlers with starter Pokemon selection."""

import random
import uuid
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.database.models import Pokemon, PokemonSpecies, User
from telemon.config import BOT_NAME, CURRENCY_NAME
from telemon.core.constants import NATURES, STARTER_LEVEL, STARTER_IV_FLOOR, MAX_IV, MAX_IV_TOTAL, determine_gender, iv_percentage, random_nature
from telemon.logging import get_logger

router = Router(name="start")
logger = get_logger(__name__)

# Gen 1 starters
STARTER_POKEMON = {
    "bulbasaur": 1,
    "charmander": 4,
    "squirtle": 7,
    "pikachu": 25,
}

WELCOME_NEW_USER = f"""
<b>Welcome to {BOT_NAME}!</b>

A Pokemon-style game right here on Telegram!

<b>Choose your starter Pokemon to begin your journey!</b>
"""

WELCOME_MESSAGE = f"""
<b>Welcome to {BOT_NAME}!</b>
A Pokemon-style game right here on Telegram!

<b>What can you do?</b>
• Catch wild Pokemon in group chats
• Train, battle, trade, and collect
• Buy and sell on the global market

<b>Quick Start:</b> Add me to a group → Chat → Pokemon spawn → /catch &lt;name&gt;

Use /help to see all commands.
"""

RETURNING_MESSAGE = f"""
<b>Welcome back, {{name}}!</b>

Pokemon caught: <b>{{pokemon_count}}</b>
Balance: <b>{{balance}}</b> {CURRENCY_NAME}

Use /pokemon to manage your collection or /help for commands.
"""


def get_starter_keyboard() -> InlineKeyboardMarkup:
    """Create inline keyboard for starter selection."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Bulbasaur", callback_data="starter:bulbasaur"),
                InlineKeyboardButton(text="Charmander", callback_data="starter:charmander"),
            ],
            [
                InlineKeyboardButton(text="Squirtle", callback_data="starter:squirtle"),
                InlineKeyboardButton(text="Pikachu", callback_data="starter:pikachu"),
            ],
        ]
    )


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /start command."""
    # Check if user has any Pokemon (not just if they're new)
    result = await session.execute(
        select(func.count(Pokemon.id)).where(Pokemon.owner_id == user.telegram_id)
    )
    pokemon_count = result.scalar() or 0

    if pokemon_count == 0:
        # New user without Pokemon - show starter selection
        await message.answer(
            WELCOME_NEW_USER,
            reply_markup=get_starter_keyboard(),
        )
    else:
        # Returning user with Pokemon
        await message.answer(
            RETURNING_MESSAGE.format(
                name=user.display_name,
                pokemon_count=pokemon_count,
                balance=user.balance,
            )
        )


@router.callback_query(F.data.startswith("starter:"))
async def callback_starter_selection(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    """Handle starter Pokemon selection."""
    try:
        if not callback.data:
            return

        starter_name = callback.data.split(":")[1]
        if starter_name not in STARTER_POKEMON:
            await callback.answer("Invalid starter!", show_alert=True)
            return

        # Check if user already has Pokemon
        result = await session.execute(
            select(func.count(Pokemon.id)).where(Pokemon.owner_id == user.telegram_id)
        )
        pokemon_count = result.scalar() or 0

        if pokemon_count > 0:
            await callback.answer("You already have Pokemon!", show_alert=True)
            return

        # Get the species
        species_id = STARTER_POKEMON[starter_name]
        result = await session.execute(
            select(PokemonSpecies).where(PokemonSpecies.national_dex == species_id)
        )
        species = result.scalar_one_or_none()

        if not species:
            await callback.answer("Species not found!", show_alert=True)
            return

        # Generate starter stats (slightly better IVs for starters)
        ivs = {
            "hp": random.randint(STARTER_IV_FLOOR, MAX_IV),
            "attack": random.randint(STARTER_IV_FLOOR, MAX_IV),
            "defense": random.randint(STARTER_IV_FLOOR, MAX_IV),
            "sp_attack": random.randint(STARTER_IV_FLOOR, MAX_IV),
            "sp_defense": random.randint(STARTER_IV_FLOOR, MAX_IV),
            "speed": random.randint(STARTER_IV_FLOOR, MAX_IV),
        }

        nature = random_nature()

        abilities = species.abilities or ["unknown"]
        ability = random.choice(abilities)

        gender = determine_gender(species)

        # Create the starter Pokemon at level 5
        starter = Pokemon(
            id=uuid.uuid4(),
            owner_id=user.telegram_id,
            species_id=species_id,
            level=STARTER_LEVEL,
            iv_hp=ivs["hp"],
            iv_attack=ivs["attack"],
            iv_defense=ivs["defense"],
            iv_sp_attack=ivs["sp_attack"],
            iv_sp_defense=ivs["sp_defense"],
            iv_speed=ivs["speed"],
            nature=nature,
            ability=ability,
            is_shiny=False,
            gender=gender,
            original_trainer_id=user.telegram_id,
            is_favorite=True,  # Starter is automatically favorited
        )

        session.add(starter)
        user.selected_pokemon_id = str(starter.id)  # Convert UUID to string

        # Explicitly set species relationship since it's not loaded on a new object
        starter.species = species

        # Assign initial moves based on species learnset
        from telemon.core.moves import assign_starter_moves

        await assign_starter_moves(session, starter)

        await session.commit()

        iv_total = sum(ivs.values())
        iv_percent = iv_percentage(iv_total)

        # Edit the original message
        await callback.message.edit_text(
            f"<b>Congratulations, {user.display_name}!</b>\n\n"
            f"You chose <b>{species.name}</b> as your starter!\n"
            f"Lv.5 | IV: {iv_percent}% | {nature.capitalize()} | {ability} | {gender or 'Unknown'}\n\n"
            f"<i>Add me to a group chat to start catching more Pokemon!</i>\n"
            f"Use /help for all commands."
        )

        await callback.answer(f"You chose {species.name}!")

        logger.info(
            "User selected starter",
            user_id=user.telegram_id,
            starter=species.name,
        )

    except Exception as e:
        logger.error(
            "Starter selection failed",
            error=str(e),
            error_type=type(e).__name__,
            user_id=callback.from_user.id if callback.from_user else None,
            callback_data=callback.data,
            exc_info=True,
        )
        try:
            await callback.answer("Something went wrong! Please try again.", show_alert=True)
        except Exception:
            pass


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    """Simple ping command to test bot responsiveness."""
    await message.answer("Pong!")
