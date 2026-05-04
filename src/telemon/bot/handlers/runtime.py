"""Runtime restart and rebuild commands. Bot owner only."""

import sys

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from redis.asyncio import Redis

from telemon.config import BOT_OWNER_ID, settings
from telemon.logging import get_logger

router = Router(name="runtime")
logger = get_logger(__name__)


@router.message(Command("restart"))
async def cmd_restart(message: Message) -> None:
    """Plain restart — exit the process; Docker restarts the container.

    Since ``./src`` is volume-mounted into the container, a restart
    automatically picks up any local code changes without a rebuild.
    """
    if not message.from_user or message.from_user.id != BOT_OWNER_ID:
        return  # silently ignore non-owners

    logger.info(
        "Owner requested plain restart",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
    )

    await message.answer(
        "🔄 <b>Restarting bot…</b>\n\n"
        "The process will exit and Docker will bring it back online "
        "in a few seconds.\n"
        "Any local code changes will be picked up automatically."
    )

    # Graceful exit — Docker's `restart: unless-stopped` will respawn us.
    sys.exit(0)


@router.message(Command("rebuild"))
async def cmd_rebuild(message: Message) -> None:
    """Rebuild & restart — signal the host watchdog via Redis.

    The host-side ``scripts/rebuild_watchdog.sh`` polls Redis for the
    ``REBUILD_REQUESTED`` key.  When found it runs
    ``docker compose up -d --build bot`` which rebuilds the image
    (using cached pip layers) and restarts the container.  Only needed
    when dependencies in ``pyproject.toml`` change — code changes are
    picked up by a plain ``/restart`` via the volume mount.
    """
    if not message.from_user or message.from_user.id != BOT_OWNER_ID:
        return  # silently ignore non-owners

    logger.info(
        "Owner requested rebuild & restart",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
    )

    # Set the rebuild flag in Redis so the host watchdog picks it up.
    redis = Redis.from_url(str(settings.redis_url))
    try:
        await redis.set("REBUILD_REQUESTED", "1")
    finally:
        await redis.aclose()

    await message.answer(
        "🏗️ <b>Rebuild requested!</b>\n\n"
        "The host watchdog will rebuild the Docker image and restart "
        "the container.\n"
        "This is only needed for dependency changes — code changes "
        "are picked up by <code>/restart</code>.\n"
        "Usually takes under 10 seconds (packages are cached).\n\n"
        "<i>If nothing happens, make sure "
        "<code>scripts/rebuild_watchdog.sh</code> is running on the host.</i>"
    )

    # Exit so Docker stops this container while the watchdog rebuilds.
    sys.exit(0)
