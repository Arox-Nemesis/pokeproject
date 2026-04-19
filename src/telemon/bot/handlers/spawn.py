"""Spawn-related handlers - message tracking and spawn triggers."""

import time
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.config import settings
from telemon.core.emoji import rarity_emoji
from telemon.core.spawning import check_spawn_trigger, create_spawn, get_random_species
from telemon.database.models import ActiveSpawn, Group, PokemonSpecies
from telemon.logging import get_logger

router = Router(name="spawn")
logger = get_logger(__name__)

# In-memory cooldown tracking (simple approach, reset on bot restart)
# For production, use Redis for persistence across restarts
_user_cooldowns: dict[int, float] = {}
_guild_cooldowns: dict[int, float] = {}

# Max entries before pruning expired cooldowns to prevent memory leaks
_COOLDOWN_MAX_SIZE = 1000


def _check_user_cooldown(user_id: int) -> bool:
    """Check if user is on cooldown. Returns True if message should count."""
    current = time.time()
    last_time = _user_cooldowns.get(user_id, 0)
    
    if current - last_time < settings.spawn_user_cooldown_seconds:
        return False  # On cooldown, don't count
    
    _user_cooldowns[user_id] = current
    return True


def _check_guild_cooldown(guild_id: int) -> bool:
    """Check if guild is on cooldown. Returns True if message should count."""
    current = time.time()
    last_time = _guild_cooldowns.get(guild_id, 0)
    
    if current - last_time < settings.spawn_guild_cooldown_seconds:
        return False  # On cooldown, don't count
    
    _guild_cooldowns[guild_id] = current
    return True


def _prune_cooldowns() -> None:
    """Remove expired entries from cooldown dicts to prevent memory leaks."""
    current = time.time()

    if len(_user_cooldowns) > _COOLDOWN_MAX_SIZE:
        expired = [
            uid for uid, ts in _user_cooldowns.items()
            if current - ts > settings.spawn_user_cooldown_seconds
        ]
        for uid in expired:
            del _user_cooldowns[uid]

    if len(_guild_cooldowns) > _COOLDOWN_MAX_SIZE:
        expired = [
            gid for gid, ts in _guild_cooldowns.items()
            if current - ts > settings.spawn_guild_cooldown_seconds
        ]
        for gid in expired:
            del _guild_cooldowns[gid]


def _is_valid_message(message: Message) -> bool:
    """Check if message is valid for spawn counting (anti-spam)."""
    # Must have text content
    if not message.text:
        return False
    
    # Must meet minimum length
    if len(message.text.strip()) < settings.spawn_min_message_length:
        return False
    
    # Skip commands
    if message.text.startswith("/"):
        return False
    
    # Skip messages that are just emojis or special chars
    # Allow if at least 2 alphanumeric characters
    alphanum_count = sum(1 for c in message.text if c.isalnum())
    if alphanum_count < 2:
        return False
    
    return True


async def send_spawn_message(bot: Bot, chat_id: int, spawn: ActiveSpawn) -> int | None:
    """Send a spawn message with Pokemon image and return message ID."""
    from aiogram.types import BufferedInputFile
    from telemon.core.imaging import generate_spawn_image

    species = spawn.species

    # Build spawn message
    shiny_text = " ✨ SHINY!" if spawn.is_shiny else ""
    rarity_text = get_rarity_text(species)

    caption = (
        f"<b>A wild Pokémon has appeared!</b>{shiny_text}\n\n"
        f"Type <code>/catch [name]</code> to catch it!\n"
        f"Use <code>/hint</code> if you need help.\n"
        f"<i>It will flee in {settings.spawn_timeout_seconds // 60} minutes...</i>"
    )

    try:
        # Generate spawn image with typed background
        image_data = await generate_spawn_image(
            dex_number=species.national_dex,
            primary_type=species.type1 or "normal",
            shiny=spawn.is_shiny,
        )

        if image_data:
            # Send generated image as file upload
            photo = BufferedInputFile(
                file=image_data.read(),
                filename=f"spawn_{species.national_dex}.jpg",
            )
            msg = await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
            )
        elif species.sprite_url:
            # Fallback to direct URL
            msg = await bot.send_photo(
                chat_id=chat_id,
                photo=species.sprite_url,
                caption=caption,
            )
        else:
            # Fallback to text only
            msg = await bot.send_message(
                chat_id=chat_id,
                text=caption,
            )
        return msg.message_id
    except Exception as e:
        logger.error("Failed to send spawn message", error=str(e), chat_id=chat_id)
        return None


def get_rarity_text(species: PokemonSpecies) -> str:
    """Get rarity text based on Pokemon rarity."""
    return rarity_emoji(species)


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def track_group_message(
    message: Message,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Track messages in groups and trigger spawns."""
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    # Anti-spam: skip anonymous admins entirely (user_id 0)
    if user_id == 0:
        return

    # Anti-spam: Check if message is valid for counting
    if not _is_valid_message(message):
        return

    # Anti-spam: Check user cooldown (1.5 sec between messages counting)
    if not _check_user_cooldown(user_id):
        return

    # Anti-spam: Check guild cooldown (1 sec between any messages counting)
    if not _check_guild_cooldown(chat_id):
        return

    # Periodically prune cooldown dicts to prevent memory leaks
    _prune_cooldowns()

    # Get or create group
    result = await session.execute(
        select(Group).where(Group.chat_id == chat_id)
    )
    group = result.scalar_one_or_none()

    if not group:
        group = Group(
            chat_id=chat_id,
            title=message.chat.title,
            bot_joined_at=datetime.utcnow(),
        )
        session.add(group)
        await session.flush()

    if not group.spawn_enabled:
        return

    # Increment message count
    group.message_count += 1

    # Decrement egg steps for this user (best-effort)
    try:
        from telemon.core.breeding import add_steps_to_eggs

        ready_eggs = await add_steps_to_eggs(session, user_id, steps=1)
        if ready_eggs:
            try:
                egg_names = []
                for egg in ready_eggs:
                    name = egg.species.name if egg.species else "an egg"
                    egg_names.append(name)
                names_text = ", ".join(egg_names)
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🥚 Your egg{'s' if len(ready_eggs) > 1 else ''} "
                        f"({names_text}) {'are' if len(ready_eggs) > 1 else 'is'} "
                        f"ready to hatch!\nUse /hatch to hatch {'them' if len(ready_eggs) > 1 else 'it'}!"
                    ),
                )
            except Exception as e:
                logger.warning("Failed to send egg hatch DM", error=str(e), user_id=user_id)
    except Exception as e:
        logger.warning(
            "Egg step tracking failed", error=str(e), user_id=user_id, chat_id=chat_id
        )
    
    # Log every 5 messages for debugging (reduce spam)
    if group.message_count % 5 == 0:
        logger.info(
            "Message count update",
            chat_id=chat_id,
            message_count=group.message_count,
            threshold=group.spawn_threshold,
        )

    # Flush the increment before checking spawn trigger
    await session.flush()

    # Check if we should spawn
    should_spawn = await check_spawn_trigger(session, chat_id)

    if should_spawn:
        # Get random species
        species = await get_random_species(session)
        if species:
            # Create spawn record (without message_id for now)
            spawn = await create_spawn(
                session=session,
                chat_id=chat_id,
                message_id=0,  # Will update after sending
                species=species,
            )

            if spawn:
                # Send spawn message
                msg_id = await send_spawn_message(bot, chat_id, spawn)
                if msg_id:
                    spawn.message_id = msg_id

                    # Only update spawn stats after confirmed delivery
                    group.total_spawns += 1
                    group.last_spawn_at = datetime.utcnow()

                    await session.commit()

                    logger.info(
                        "Pokemon spawned",
                        chat_id=chat_id,
                        species=species.name,
                        is_shiny=spawn.is_shiny,
                        rarity="legendary" if species.is_legendary else "mythical" if species.is_mythical else "normal",
                    )
                    return
                else:
                    # Message send failed — remove the ghost spawn record
                    await session.delete(spawn)
                    logger.warning(
                        "Spawn message send failed, removing spawn record",
                        chat_id=chat_id,
                        species=species.name,
                    )

    await session.commit()
