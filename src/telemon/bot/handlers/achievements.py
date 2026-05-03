"""Achievements command handler."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telemon.config import CURRENCY_SHORT
from telemon.core.achievements import ACHIEVEMENTS
from telemon.database.models.achievement import UserAchievement
from telemon.database.models.user import User

router = Router(name="achievements")

# Categories for display ordering
CATEGORY_ORDER = [
    ("catch", "Catching", "🎯"),
    ("shiny", "Shiny", "✨"),
    ("pokedex", "Pokedex", "📖"),
    ("evolution", "Evolution", "🔄"),
    ("battle", "Battle", "⚔️"),
    ("trade", "Trading", "🤝"),
    ("streak", "Daily Streak", "🔥"),
    ("special", "Special", "🏆"),
    ("wonder", "Wonder Trade", "🎁"),
]


async def _get_unlocked(session: AsyncSession, user_id: int) -> set[str]:
    """Get set of unlocked achievement IDs for a user."""
    result = await session.execute(
        select(UserAchievement.achievement_id).where(
            UserAchievement.user_id == user_id
        )
    )
    return set(result.scalars().all())


def _build_ach_overview(unlocked: set[str]) -> str:
    """Build the achievements overview text."""
    total = len(ACHIEVEMENTS)
    earned = len(unlocked)
    total_tc = sum(
        ach["reward"] for aid, ach in ACHIEVEMENTS.items() if aid in unlocked
    )

    lines = [
        f"<b>Achievements</b> — {earned}/{total}\n",
    ]

    for cat_id, cat_name, emoji in CATEGORY_ORDER:
        cat_achs = [
            (aid, ach) for aid, ach in ACHIEVEMENTS.items()
            if ach["category"] == cat_id
        ]
        if not cat_achs:
            continue
        cat_earned = sum(1 for aid, _ in cat_achs if aid in unlocked)
        lines.append(f"  {emoji} {cat_name}: {cat_earned}/{len(cat_achs)}")

    lines.append(f"\n<b>Total earned:</b> {total_tc:,} {CURRENCY_SHORT}")
    lines.append("\n<i>Tap a category below to see details.</i>")

    return "\n".join(lines)


def _build_ach_keyboard(unlocked: set[str]) -> InlineKeyboardBuilder:
    """Build the achievement category keyboard."""
    builder = InlineKeyboardBuilder()
    for cat_id, cat_name, emoji in CATEGORY_ORDER:
        cat_achs = [
            aid for aid, ach in ACHIEVEMENTS.items() if ach["category"] == cat_id
        ]
        if not cat_achs:
            continue
        cat_earned = sum(1 for aid in cat_achs if aid in unlocked)
        builder.button(
            text=f"{emoji} {cat_name} ({cat_earned}/{len(cat_achs)})",
            callback_data=f"ach:{cat_id}",
        )
    builder.adjust(2)
    return builder


def _build_category_text(cat_id: str, cat_name: str, emoji: str, unlocked: set[str]) -> str:
    """Build the text for a single achievement category."""
    cat_achs = [
        (aid, ach) for aid, ach in ACHIEVEMENTS.items()
        if ach["category"] == cat_id
    ]
    if not cat_achs:
        return f"{emoji} <b>{cat_name}</b>\n\nNo achievements in this category."

    cat_earned = sum(1 for aid, _ in cat_achs if aid in unlocked)
    lines = [
        f"{emoji} <b>{cat_name}</b> — {cat_earned}/{len(cat_achs)}\n",
    ]

    for aid, ach in cat_achs:
        mark = "+" if aid in unlocked else "-"
        reward_str = f"{ach['reward']:,}"
        lines.append(
            f"  [{mark}] {ach['name']} — {ach['desc']} ({reward_str} {CURRENCY_SHORT})"
        )

    return "\n".join(lines)


def _ach_back_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Back to overview", callback_data="ach:back")
    return builder


@router.message(Command("achievements", "badges", "ach"))
async def cmd_achievements(
    message: Message, session: AsyncSession, user: User
) -> None:
    """Show the user's achievement progress."""
    unlocked = await _get_unlocked(session, user.telegram_id)
    overview = _build_ach_overview(unlocked)
    keyboard = _build_ach_keyboard(unlocked)
    await message.answer(overview, reply_markup=keyboard.as_markup())


@router.callback_query(F.data.startswith("ach:"))
async def callback_achievements(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle achievement category selection."""
    data = (callback.data or "").split(":", 1)
    if len(data) < 2:
        await callback.answer()
        return

    key = data[1]
    unlocked = await _get_unlocked(session, user.telegram_id)

    if key == "back":
        overview = _build_ach_overview(unlocked)
        keyboard = _build_ach_keyboard(unlocked)
        await callback.message.edit_text(
            overview, reply_markup=keyboard.as_markup()
        )
        await callback.answer()
        return

    # Find the category
    cat_match = None
    for cat_id, cat_name, emoji in CATEGORY_ORDER:
        if cat_id == key:
            cat_match = (cat_id, cat_name, emoji)
            break

    if not cat_match:
        await callback.answer("Unknown category")
        return

    text = _build_category_text(cat_match[0], cat_match[1], cat_match[2], unlocked)
    await callback.message.edit_text(
        text, reply_markup=_ach_back_keyboard().as_markup()
    )
    await callback.answer()
