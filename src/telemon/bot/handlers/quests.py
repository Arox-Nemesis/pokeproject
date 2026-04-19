"""Quest-related handlers for daily/weekly tasks."""

from datetime import datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.config import CURRENCY_SHORT
from telemon.core.quests import claim_quest, get_or_create_quests
from telemon.database.models import User
from telemon.logging import get_logger

router = Router(name="quests")
logger = get_logger(__name__)


def _time_remaining(expires_at: datetime) -> str:
    """Format time remaining until expiry."""
    remaining = expires_at - datetime.utcnow()
    if remaining.total_seconds() <= 0:
        return "Expired"
    hours = int(remaining.total_seconds() // 3600)
    minutes = int((remaining.total_seconds() % 3600) // 60)
    if hours > 24:
        days = hours // 24
        hours = hours % 24
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m"


def _progress_bar(current: int, target: int, width: int = 8) -> str:
    """Generate a small progress bar."""
    ratio = min(current / target, 1.0) if target > 0 else 0
    filled = int(ratio * width)
    empty = width - filled
    return "█" * filled + "░" * empty


@router.message(Command("quest", "quests", "q"))
async def cmd_quests(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /quest command to view and manage quests."""
    text = message.text or ""
    args = text.split()

    if len(args) >= 2:
        sub = args[1].lower()
        if sub == "claim":
            await handle_claim(message, session, user, args)
            return
        elif sub == "claimall":
            await handle_claim_all(message, session, user)
            return
        elif sub == "help":
            await quest_help(message)
            return

    # Show quest overview
    daily_quests, weekly_quests = await get_or_create_quests(session, user.telegram_id)

    lines = ["<b>📋 Your Quests</b>\n"]

    # Daily quests
    if daily_quests:
        reset_time = _time_remaining(daily_quests[0].expires_at)
        lines.append(f"<b>Daily Quests</b> (resets in {reset_time})")
        for i, quest in enumerate(daily_quests, 1):
            status = _format_quest_line(quest, i)
            lines.append(status)
    else:
        lines.append("<b>Daily Quests</b>\n  <i>No daily quests available</i>")

    lines.append("")

    # Weekly quests
    if weekly_quests:
        reset_time = _time_remaining(weekly_quests[0].expires_at)
        lines.append(f"<b>Weekly Quests</b> (resets in {reset_time})")
        for i, quest in enumerate(weekly_quests, 1):
            status = _format_quest_line(quest, i)
            lines.append(status)
    else:
        lines.append("<b>Weekly Quests</b>\n  <i>No weekly quests available</i>")

    # Count claimable
    claimable = [q for q in daily_quests + weekly_quests if q.is_completed and not q.is_claimed]
    if claimable:
        total_reward = sum(q.reward_coins for q in claimable)
        lines.append(f"\n<b>{len(claimable)} quest(s) ready to claim!</b> ({total_reward:,} {CURRENCY_SHORT})")
        lines.append("Use /quest claimall to claim all rewards")

    await message.answer("\n".join(lines))


def _format_quest_line(quest, index: int) -> str:
    """Format a single quest line."""
    if quest.is_claimed:
        return f"  {index}. ✅ <s>{quest.description}</s> — <i>Claimed!</i>"
    elif quest.is_completed:
        return f"  {index}. 🎉 {quest.description} — <b>+{quest.reward_coins:,} {CURRENCY_SHORT}</b> (ready!)"
    else:
        bar = _progress_bar(quest.current_count, quest.target_count)
        return (
            f"  {index}. {quest.description}\n"
            f"       [{bar}] {quest.current_count}/{quest.target_count} — {quest.reward_coins:,} {CURRENCY_SHORT}"
        )


async def handle_claim(
    message: Message, session: AsyncSession, user: User, args: list[str]
) -> None:
    """Handle /quest claim [number] — claim a specific quest."""
    # For simplicity, claim all completed quests
    await handle_claim_all(message, session, user)


async def handle_claim_all(
    message: Message, session: AsyncSession, user: User
) -> None:
    """Claim all completed quests at once."""
    daily_quests, weekly_quests = await get_or_create_quests(session, user.telegram_id)
    all_quests = daily_quests + weekly_quests

    claimable = [q for q in all_quests if q.is_completed and not q.is_claimed]

    if not claimable:
        await message.answer(
            "No completed quests to claim!\n"
            "Use /quest to see your current quests."
        )
        return

    total_reward = 0
    claimed_lines = []

    for quest in claimable:
        success, desc, reward = await claim_quest(session, user.telegram_id, str(quest.id))
        if success:
            total_reward += reward
            claimed_lines.append(f"  ✅ {desc} — +{reward:,} {CURRENCY_SHORT}")

    user.balance += total_reward
    await session.commit()

    await message.answer(
        f"<b>Quests Claimed!</b>\n\n"
        + "\n".join(claimed_lines)
        + f"\n\n<b>Total:</b> +{total_reward:,} {CURRENCY_SHORT}\n"
        f"<b>Balance:</b> {user.balance:,} {CURRENCY_SHORT}"
    )

    logger.info(
        "User claimed quest rewards",
        user_id=user.telegram_id,
        quests_claimed=len(claimable),
        total_reward=total_reward,
    )


async def quest_help(message: Message) -> None:
    """Show quest help."""
    await message.answer(
        "<b>📋 Quest Commands</b>\n\n"
        "/quest - View your daily & weekly quests\n"
        "/quest claimall - Claim all completed quest rewards\n"
        "/quest help - Show this help\n\n"
        "<b>How Quests Work:</b>\n"
        "- You get 3 daily quests (reset at midnight UTC)\n"
        "- You get 2 weekly quests (reset Monday midnight UTC)\n"
        "- Complete tasks to fill the progress bar\n"
        "- Claim rewards when quests are complete\n"
        "- Unclaimed quests are lost when they expire\n\n"
        "<b>Quest Types:</b>\n"
        "- Catch Pokemon (general or by type)\n"
        "- Win battles\n"
        "- Pet your Pokemon (friendship)\n"
        "- Evolve Pokemon\n"
        "- Complete trades\n"
        "- Sell on market\n"
        "- Claim daily reward\n"
        "- Use items"
    )
