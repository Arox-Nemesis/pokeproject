"""DM notification system for asynchronous user alerts."""

from __future__ import annotations

from aiogram import Bot

from telemon.core.text import esc
from telemon.logging import get_logger

logger = get_logger(__name__)


async def notify_user(bot: Bot, user_id: int, text: str) -> bool:
    """Send a DM notification to a user. Returns True on success."""
    try:
        await bot.send_message(chat_id=user_id, text=text)
        return True
    except Exception as e:
        # User may have blocked the bot or never started a DM
        logger.debug(
            "Failed to send DM notification",
            user_id=user_id,
            error=str(e),
        )
        return False


async def notify_market_sale(
    bot: Bot,
    seller_id: int,
    pokemon_name: str,
    price: int,
    buyer_name: str,
) -> None:
    """Notify seller that their market listing was purchased."""
    await notify_user(
        bot,
        seller_id,
        f"<b>Market Sale!</b>\n\n"
        f"Your <b>{pokemon_name}</b> was purchased by {esc(buyer_name)} "
        f"for <b>{price:,} TC</b>!\n\n"
        f"The coins have been added to your balance.",
    )


async def notify_wonder_trade_match(
    bot: Bot,
    user_id: int,
    sent_name: str,
    received_name: str,
    received_level: int,
    received_iv: float,
    is_shiny: bool,
) -> None:
    """Notify a user that their wonder trade was matched."""
    shiny = " ✨" if is_shiny else ""
    await notify_user(
        bot,
        user_id,
        f"<b>Wonder Trade Matched!</b>\n\n"
        f"Your <b>{sent_name}</b> was traded!\n"
        f"You received: <b>{received_name}</b>{shiny} "
        f"(Lv.{received_level}, {received_iv:.1f}% IV)\n\n"
        f"Check /pokemon to see your new Pokemon!",
    )
