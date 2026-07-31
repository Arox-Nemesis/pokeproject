"""Shiny hunting handlers for chaining and improved odds."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.core.shiny import (
    CHAIN_TIERS,
    chain_divisor,
    format_odds,
    has_shiny_charm,
    shiny_rate,
)
from telemon.database.models import PokedexEntry, PokemonSpecies, User
from telemon.logging import get_logger
from telemon.core.text import esc

router = Router(name="shinyhunt")
logger = get_logger(__name__)

# Chain milestones shown in /hunt odds, derived from the odds the catch handler
# actually rolls (core.shiny) so the table can never drift from the maths.
CHAIN_THRESHOLDS = [
    (threshold, divisor, format_odds(threshold)) for threshold, divisor in CHAIN_TIERS
]


def get_chain_info(chain: int) -> tuple[float, str]:
    """Get multiplier and odds string for a chain count."""
    return chain_divisor(chain), format_odds(chain)


def calculate_shiny_odds(chain: int, has_charm: bool = False) -> tuple[int, str]:
    """Calculate actual shiny denominator and display string."""
    rate = shiny_rate(chain, has_charm)
    return rate, f"1/{rate}"


async def get_species_by_query(
    session: AsyncSession, query: str
) -> PokemonSpecies | None:
    """Find Pokemon species by name or dex number."""
    if query.isdigit():
        result = await session.execute(
            select(PokemonSpecies).where(PokemonSpecies.national_dex == int(query))
        )
        return result.scalar_one_or_none()
    
    # Try exact match first
    result = await session.execute(
        select(PokemonSpecies).where(PokemonSpecies.name.ilike(query))
    )
    species = result.scalar_one_or_none()
    if species:
        return species
    
    # Try partial match
    result = await session.execute(
        select(PokemonSpecies).where(PokemonSpecies.name.ilike(f"%{query}%")).limit(1)
    )
    return result.scalar_one_or_none()


@router.message(Command("hunt", "shinyhunt", "sh"))
async def cmd_hunt(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /hunt command for shiny hunting."""
    text = message.text or ""
    args = text.split()
    
    if len(args) < 2:
        # Show current hunt status or help
        if user.shiny_hunt_species_id:
            await show_hunt_status(message, session, user)
        else:
            await show_hunt_help(message)
        return
    
    subcommand = args[1].lower()
    
    if subcommand in ["status", "info", "check"]:
        await show_hunt_status(message, session, user)
    elif subcommand in ["stop", "end", "cancel", "quit"]:
        await stop_hunt(message, session, user)
    elif subcommand in ["odds", "rates", "chances"]:
        await show_odds_table(message, session, user)
    elif subcommand == "help":
        await show_hunt_help(message)
    else:
        # Start hunting a specific Pokemon
        species_query = " ".join(args[1:])
        await start_hunt(message, session, user, species_query)


async def show_hunt_help(message: Message) -> None:
    """Show shiny hunting help."""
    await message.answer(
        "✨ <b>Shiny Hunting Guide</b>\n\n"
        "<b>What is Shiny Hunting?</b>\n"
        "Focus on catching one species repeatedly to increase\n"
        "your chances of finding a shiny version!\n\n"
        "<b>Commands:</b>\n"
        "/hunt [pokemon] - Start hunting a species\n"
        "/hunt status - Check your current chain\n"
        "/hunt stop - End your current hunt\n"
        "/hunt odds - View odds table\n\n"
        "<b>How Chains Work:</b>\n"
        "• Catch your target Pokemon = +1 chain\n"
        "• Catch a different Pokemon = chain breaks!\n"
        "• Let a spawn flee = no penalty\n"
        "• Higher chain = better shiny odds\n\n"
        "<b>Chain Bonuses:</b>\n"
        "10+ catches: ~1.5x odds\n"
        "25+ catches: 2x odds (1/2048)\n"
        "100+ catches: 4x odds (1/1024)\n"
        "200+ catches: 8x odds (1/512)\n\n"
        "<b>Shiny Charm:</b>\n"
        "A special item that further triples your odds!\n"
        "Check the shop for availability."
    )


async def start_hunt(
    message: Message, session: AsyncSession, user: User, species_query: str
) -> None:
    """Start hunting a specific Pokemon species."""
    # Find the species
    species = await get_species_by_query(session, species_query)
    
    if not species:
        await message.answer(
            f"❌ Pokemon '{esc(species_query)}' not found.\n"
            "Use the exact name or Dex number."
        )
        return
    
    # Check if already hunting this species
    if user.shiny_hunt_species_id == species.national_dex:
        chain = user.shiny_hunt_chain
        _, odds = calculate_shiny_odds(chain, await has_shiny_charm(session, user.telegram_id))
        await message.answer(
            f"✨ You're already hunting <b>{species.name}</b>!\n\n"
            f"Current chain: {chain}\n"
            f"Current odds: {odds}\n\n"
            f"Keep catching {species.name} to increase your chain!"
        )
        return
    
    # Check if switching hunts
    old_species_name = None
    old_chain = 0
    if user.shiny_hunt_species_id:
        old_result = await session.execute(
            select(PokemonSpecies).where(
                PokemonSpecies.national_dex == user.shiny_hunt_species_id
            )
        )
        old_species = old_result.scalar_one_or_none()
        if old_species:
            old_species_name = old_species.name
            old_chain = user.shiny_hunt_chain
    
    # Set new hunt
    user.shiny_hunt_species_id = species.national_dex
    user.shiny_hunt_chain = 0
    await session.commit()
    
    logger.info(
        "Shiny hunt started",
        user_id=user.telegram_id,
        species_id=species.national_dex,
        species_name=species.name,
    )
    
    # Check if user has caught this species before
    dex_result = await session.execute(
        select(PokedexEntry)
        .where(PokedexEntry.user_id == user.telegram_id)
        .where(PokedexEntry.species_id == species.national_dex)
    )
    dex_entry = dex_result.scalar_one_or_none()
    
    has_charm = await has_shiny_charm(session, user.telegram_id)
    _, base_odds = calculate_shiny_odds(0, has_charm)
    
    switch_msg = ""
    if old_species_name:
        switch_msg = (
            f"⚠️ <i>Switched from {old_species_name} (chain: {old_chain})</i>\n\n"
        )
    
    caught_msg = ""
    if dex_entry and dex_entry.caught:
        caught_msg = f"You've caught {dex_entry.times_caught} before.\n"
        if dex_entry.caught_shiny:
            caught_msg += "✨ You already have a shiny!\n"
    
    charm_msg = "🎀 Shiny Charm active!" if has_charm else ""
    
    await message.answer(
        f"✨ <b>Shiny Hunt Started!</b>\n\n"
        f"{switch_msg}"
        f"Target: <b>{species.name}</b> (#{species.national_dex})\n"
        f"Chain: 0\n"
        f"Starting odds: {base_odds}\n"
        f"{caught_msg}"
        f"{charm_msg}\n\n"
        f"Catch <b>{species.name}</b> to build your chain!\n"
        f"<i>Catching other Pokemon will break your chain.</i>"
    )


async def show_hunt_status(message: Message, session: AsyncSession, user: User) -> None:
    """Show current shiny hunt status."""
    if not user.shiny_hunt_species_id:
        await message.answer(
            "✨ <b>Shiny Hunt Status</b>\n\n"
            "You're not currently hunting any Pokemon.\n\n"
            "Use /hunt [pokemon] to start!"
        )
        return
    
    # Get species info
    result = await session.execute(
        select(PokemonSpecies).where(
            PokemonSpecies.national_dex == user.shiny_hunt_species_id
        )
    )
    species = result.scalar_one_or_none()
    
    if not species:
        # Invalid hunt, reset
        user.shiny_hunt_species_id = None
        user.shiny_hunt_chain = 0
        await session.commit()
        await message.answer("❌ Hunt data was corrupted. Please start a new hunt.")
        return
    
    chain = user.shiny_hunt_chain
    has_charm = await has_shiny_charm(session, user.telegram_id)
    _, current_odds = calculate_shiny_odds(chain, has_charm)
    
    # Calculate next milestone
    next_milestone = None
    next_odds = None
    for threshold, _, odds_str in CHAIN_THRESHOLDS:
        if chain < threshold:
            next_milestone = threshold
            _, next_odds = calculate_shiny_odds(threshold, has_charm)
            break
    
    # Progress bar to next milestone
    progress_bar = ""
    if next_milestone:
        prev_milestone = 0
        for threshold, _, _ in CHAIN_THRESHOLDS:
            if threshold < next_milestone:
                prev_milestone = threshold
        
        progress = chain - prev_milestone
        total = next_milestone - prev_milestone
        filled = int((progress / total) * 10)
        progress_bar = f"\n[{'█' * filled}{'░' * (10 - filled)}] {chain}/{next_milestone}"
    
    # Check pokedex for shiny status
    dex_result = await session.execute(
        select(PokedexEntry)
        .where(PokedexEntry.user_id == user.telegram_id)
        .where(PokedexEntry.species_id == species.national_dex)
    )
    dex_entry = dex_result.scalar_one_or_none()
    
    shiny_status = ""
    if dex_entry and dex_entry.caught_shiny:
        shiny_status = "\n✨ <b>You have a shiny!</b>"
    
    charm_status = "\n🎀 Shiny Charm: Active" if has_charm else ""
    
    next_milestone_msg = ""
    if next_milestone and next_odds:
        next_milestone_msg = f"\n\nNext milestone: {next_milestone} catches ({next_odds}){progress_bar}"
    elif chain >= 200:
        next_milestone_msg = "\n\n🎉 Maximum chain bonus reached!"
    
    await message.answer(
        f"✨ <b>Shiny Hunt Status</b>\n\n"
        f"Target: <b>{species.name}</b>\n"
        f"Chain: <b>{chain}</b>\n"
        f"Current odds: <b>{current_odds}</b>"
        f"{charm_status}"
        f"{shiny_status}"
        f"{next_milestone_msg}\n\n"
        f"<i>Keep catching {species.name} to improve odds!</i>"
    )


async def stop_hunt(message: Message, session: AsyncSession, user: User) -> None:
    """Stop the current shiny hunt."""
    if not user.shiny_hunt_species_id:
        await message.answer(
            "❌ You're not currently hunting any Pokemon.\n"
            "Use /hunt [pokemon] to start!"
        )
        return
    
    # Get species name for message
    result = await session.execute(
        select(PokemonSpecies).where(
            PokemonSpecies.national_dex == user.shiny_hunt_species_id
        )
    )
    species = result.scalar_one_or_none()
    species_name = species.name if species else "Unknown"
    chain = user.shiny_hunt_chain
    
    # Reset hunt
    user.shiny_hunt_species_id = None
    user.shiny_hunt_chain = 0
    await session.commit()
    
    logger.info(
        "Shiny hunt stopped",
        user_id=user.telegram_id,
        species_name=species_name,
        final_chain=chain,
    )
    
    await message.answer(
        f"🛑 <b>Hunt Ended</b>\n\n"
        f"You stopped hunting <b>{species_name}</b>.\n"
        f"Final chain: {chain}\n\n"
        f"Use /hunt [pokemon] to start a new hunt!"
    )


async def show_odds_table(message: Message, session: AsyncSession, user: User) -> None:
    """Show the shiny odds table."""
    has_charm = await has_shiny_charm(session, user.telegram_id)
    
    lines = [
        "✨ <b>Shiny Odds Table</b>\n",
        "<b>Chain → Odds</b>",
    ]
    
    for threshold, _, _ in CHAIN_THRESHOLDS:
        _, odds = calculate_shiny_odds(threshold, has_charm)
        marker = ""
        if user.shiny_hunt_chain and threshold <= user.shiny_hunt_chain:
            marker = " ◀️" if threshold == max(
                t for t, _, _ in CHAIN_THRESHOLDS if t <= user.shiny_hunt_chain
            ) else " ✓"
        lines.append(f"{threshold}+ catches: {odds}{marker}")
    
    charm_note = (
        "\n\n🎀 <b>Shiny Charm Active!</b>\nOdds above include your charm bonus."
        if has_charm else
        "\n\n<i>Get the Shiny Charm for 3x better odds!</i>"
    )
    
    lines.append(charm_note)
    
    await message.answer("\n".join(lines))
