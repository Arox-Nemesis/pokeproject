"""Spawning engine for wild Pokemon."""

import random
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import func

from telemon.config import settings
from telemon.database.models import ActiveSpawn, Group, PokemonSpecies
from telemon.logging import get_logger

logger = get_logger(__name__)


# Rarity weights for spawning
RARITY_WEIGHTS = {
    "common": 60,      # catch_rate > 120
    "uncommon": 25,    # catch_rate 46-120
    "rare": 10,        # catch_rate 4-45
    "ultra_rare": 4,   # catch_rate 1-3
    "legendary": 0.9,  # is_legendary
    "mythical": 0.1,   # is_mythical
}


async def get_random_species(session: AsyncSession) -> PokemonSpecies | None:
    """Get a random Pokemon species based on rarity weights."""
    # Roll for rarity
    roll = random.random() * 100
    cumulative = 0

    selected_rarity = "common"
    for rarity, weight in RARITY_WEIGHTS.items():
        cumulative += weight
        if roll <= cumulative:
            selected_rarity = rarity
            break

    # Build query based on rarity
    query = select(PokemonSpecies)

    if selected_rarity == "mythical":
        query = query.where(PokemonSpecies.is_mythical == True)
    elif selected_rarity == "legendary":
        query = query.where(PokemonSpecies.is_legendary == True)
        query = query.where(PokemonSpecies.is_mythical == False)
    elif selected_rarity == "ultra_rare":
        query = query.where(PokemonSpecies.catch_rate <= 3)
        query = query.where(PokemonSpecies.is_legendary == False)
        query = query.where(PokemonSpecies.is_mythical == False)
    elif selected_rarity == "rare":
        query = query.where(PokemonSpecies.catch_rate > 3)
        query = query.where(PokemonSpecies.catch_rate <= 45)
        query = query.where(PokemonSpecies.is_legendary == False)
    elif selected_rarity == "uncommon":
        query = query.where(PokemonSpecies.catch_rate > 45)
        query = query.where(PokemonSpecies.catch_rate <= 120)
    else:  # common
        query = query.where(PokemonSpecies.catch_rate > 120)

    # Let the database handle randomization — avoids loading all rows into memory
    query = query.order_by(func.random()).limit(1)
    result = await session.execute(query)
    species = result.scalar_one_or_none()

    if species is None:
        # Fallback: any random Pokemon
        fallback = await session.execute(
            select(PokemonSpecies).order_by(func.random()).limit(1)
        )
        species = fallback.scalar_one_or_none()

    return species


def should_be_shiny(chain_bonus: int = 0) -> bool:
    """Determine if a spawn should be shiny."""
    base_rate = settings.shiny_base_rate

    # Apply chain bonus
    if chain_bonus > 200:
        rate = base_rate // 8  # 1/512
    elif chain_bonus > 100:
        rate = base_rate // 4  # 1/1024
    elif chain_bonus > 50:
        rate = base_rate // 2  # 1/2048
    else:
        rate = base_rate  # 1/4096

    return random.randint(1, rate) == 1


async def create_spawn(
    session: AsyncSession,
    chat_id: int,
    message_id: int,
    species: PokemonSpecies | None = None,
    force_shiny: bool = False,
    force_stats: dict | None = None,
) -> ActiveSpawn | None:
    """Create a new Pokemon spawn in a chat."""
    # Get random species if not provided
    if species is None:
        species = await get_random_species(session)
        if species is None:
            logger.warning("No Pokemon species found in database")
            return None

    # Determine shiny status
    is_shiny = force_shiny or should_be_shiny()

    # Calculate expiration
    expires_at = datetime.utcnow() + timedelta(seconds=settings.spawn_timeout_seconds)

    # Create spawn
    spawn = ActiveSpawn(
        chat_id=chat_id,
        species_id=species.national_dex,
        message_id=message_id,
        is_shiny=is_shiny,
        expires_at=expires_at,
    )

    # Apply forced stats if provided
    if force_stats:
        spawn.force_level = force_stats.get("level")
        spawn.force_iv_hp = force_stats.get("iv_hp")
        spawn.force_iv_attack = force_stats.get("iv_attack")
        spawn.force_iv_defense = force_stats.get("iv_defense")
        spawn.force_iv_sp_attack = force_stats.get("iv_sp_attack")
        spawn.force_iv_sp_defense = force_stats.get("iv_sp_defense")
        spawn.force_iv_speed = force_stats.get("iv_speed")

    session.add(spawn)

    # Update group stats
    result = await session.execute(
        select(Group).where(Group.chat_id == chat_id)
    )
    group = result.scalar_one_or_none()

    if group:
        group.message_count = 0
        # Randomize next spawn threshold so users can't predict exact count
        group.spawn_threshold = random.randint(
            settings.spawn_threshold_min, settings.spawn_threshold_max
        )
        # Note: group.total_spawns and group.last_spawn_at are updated by
        # the caller only AFTER confirming the spawn message was delivered.

    # Flush but don't commit — caller controls the transaction
    await session.flush()

    logger.info(
        "Created spawn",
        chat_id=chat_id,
        species=species.name,
        is_shiny=is_shiny,
    )

    return spawn


async def get_active_spawn(session: AsyncSession, chat_id: int) -> ActiveSpawn | None:
    """Get the currently active spawn for a chat."""
    result = await session.execute(
        select(ActiveSpawn)
        .where(ActiveSpawn.chat_id == chat_id)
        .where(ActiveSpawn.caught_by.is_(None))
        .where(ActiveSpawn.expires_at > datetime.utcnow())
        .order_by(ActiveSpawn.spawned_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def check_spawn_trigger(session: AsyncSession, chat_id: int) -> bool:
    """Check if a spawn should be triggered for a chat."""
    # Get group settings
    result = await session.execute(
        select(Group).where(Group.chat_id == chat_id)
    )
    group = result.scalar_one_or_none()

    if not group or not group.spawn_enabled:
        return False

    # Check if there's already an active spawn
    active_spawn = await get_active_spawn(session, chat_id)
    if active_spawn:
        return False

    # Check message threshold (group incense halves threshold)
    effective_threshold = group.spawn_threshold
    if group.incense_until and group.incense_until > datetime.utcnow():
        effective_threshold = max(1, effective_threshold // 2)

    if group.message_count >= effective_threshold:
        # Reset counter now so that if the caller's spawn fails,
        # we don't re-trigger on every subsequent message.
        group.message_count = 0
        await session.flush()
        return True

    # Check time-based spawn (if enough time has passed and some activity)
    if group.last_spawn_at and group.message_count > 5:
        time_since_last = datetime.utcnow() - group.last_spawn_at
        min_time = timedelta(minutes=settings.spawn_time_min_minutes)
        max_time = timedelta(minutes=settings.spawn_time_max_minutes)

        if time_since_last > min_time:
            # Random chance increases as we approach max time
            progress = min(1.0, time_since_last / max_time)
            if random.random() < progress * 0.3:  # Up to 30% chance
                return True

    return False


async def increment_message_count(session: AsyncSession, chat_id: int) -> None:
    """Increment the message count for spawn tracking."""
    result = await session.execute(
        select(Group).where(Group.chat_id == chat_id)
    )
    group = result.scalar_one_or_none()

    if group:
        group.message_count += 1
        await session.commit()
