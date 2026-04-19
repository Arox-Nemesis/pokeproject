"""Achievement definitions and checking logic."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.database.models.achievement import UserAchievement
from telemon.database.models.pokedex import PokedexEntry
from telemon.database.models.pokemon import Pokemon
from telemon.database.models.user import User
from telemon.database.models.wondertrade import WonderTrade
from telemon.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Achievement definitions — all defined in code, not DB
# ---------------------------------------------------------------------------

ACHIEVEMENTS: dict[str, dict[str, Any]] = {
    # --- Catching ---
    "first_catch": {
        "name": "First Catch!",
        "desc": "Catch your first Pokemon",
        "category": "catch",
        "event": "catch",
        "threshold": 1,
        "reward": 100,
    },
    "catch_10": {
        "name": "Novice Trainer",
        "desc": "Catch 10 Pokemon",
        "category": "catch",
        "event": "catch",
        "threshold": 10,
        "reward": 200,
    },
    "catch_50": {
        "name": "Skilled Trainer",
        "desc": "Catch 50 Pokemon",
        "category": "catch",
        "event": "catch",
        "threshold": 50,
        "reward": 500,
    },
    "catch_100": {
        "name": "Expert Trainer",
        "desc": "Catch 100 Pokemon",
        "category": "catch",
        "event": "catch",
        "threshold": 100,
        "reward": 1000,
    },
    "catch_500": {
        "name": "Master Trainer",
        "desc": "Catch 500 Pokemon",
        "category": "catch",
        "event": "catch",
        "threshold": 500,
        "reward": 5000,
    },
    "catch_1000": {
        "name": "Pokemon Champion",
        "desc": "Catch 1000 Pokemon",
        "category": "catch",
        "event": "catch",
        "threshold": 1000,
        "reward": 10000,
    },
    # --- Shiny ---
    "first_shiny": {
        "name": "Lucky Find",
        "desc": "Catch your first shiny",
        "category": "shiny",
        "event": "catch_shiny",
        "threshold": 1,
        "reward": 500,
    },
    "shiny_10": {
        "name": "Shiny Collector",
        "desc": "Catch 10 shiny Pokemon",
        "category": "shiny",
        "event": "catch_shiny",
        "threshold": 10,
        "reward": 2000,
    },
    "shiny_50": {
        "name": "Shiny Hunter",
        "desc": "Catch 50 shiny Pokemon",
        "category": "shiny",
        "event": "catch_shiny",
        "threshold": 50,
        "reward": 10000,
    },
    # --- Pokedex ---
    "dex_10": {
        "name": "Researcher",
        "desc": "Register 10 species",
        "category": "pokedex",
        "event": "pokedex_update",
        "threshold": 10,
        "reward": 200,
    },
    "dex_50": {
        "name": "Field Researcher",
        "desc": "Register 50 species",
        "category": "pokedex",
        "event": "pokedex_update",
        "threshold": 50,
        "reward": 1000,
    },
    "dex_151": {
        "name": "Kanto Complete",
        "desc": "Register 151 species",
        "category": "pokedex",
        "event": "pokedex_update",
        "threshold": 151,
        "reward": 5000,
    },
    "dex_500": {
        "name": "Pokemon Professor",
        "desc": "Register 500 species",
        "category": "pokedex",
        "event": "pokedex_update",
        "threshold": 500,
        "reward": 15000,
    },
    "dex_1025": {
        "name": "Living Pokedex",
        "desc": "Complete the Pokedex",
        "category": "pokedex",
        "event": "pokedex_update",
        "threshold": 1025,
        "reward": 50000,
    },
    # --- Evolution ---
    "first_evolve": {
        "name": "Metamorphosis",
        "desc": "Evolve your first Pokemon",
        "category": "evolution",
        "event": "evolve",
        "threshold": 1,
        "reward": 200,
    },
    "evolve_10": {
        "name": "Evolution Expert",
        "desc": "Evolve 10 Pokemon",
        "category": "evolution",
        "event": "evolve",
        "threshold": 10,
        "reward": 500,
    },
    "evolve_50": {
        "name": "Adaptation",
        "desc": "Evolve 50 Pokemon",
        "category": "evolution",
        "event": "evolve",
        "threshold": 50,
        "reward": 2000,
    },
    # --- Battle ---
    "first_win": {
        "name": "First Victory",
        "desc": "Win your first battle",
        "category": "battle",
        "event": "battle_win",
        "threshold": 1,
        "reward": 200,
    },
    "battle_10": {
        "name": "Competitor",
        "desc": "Win 10 battles",
        "category": "battle",
        "event": "battle_win",
        "threshold": 10,
        "reward": 500,
    },
    "battle_50": {
        "name": "Arena Star",
        "desc": "Win 50 battles",
        "category": "battle",
        "event": "battle_win",
        "threshold": 50,
        "reward": 2000,
    },
    "battle_100": {
        "name": "Battle Legend",
        "desc": "Win 100 battles",
        "category": "battle",
        "event": "battle_win",
        "threshold": 100,
        "reward": 5000,
    },
    # --- Trading ---
    "first_trade": {
        "name": "Pen Pal",
        "desc": "Complete your first trade",
        "category": "trade",
        "event": "trade",
        "threshold": 1,
        "reward": 200,
    },
    "trade_10": {
        "name": "Trader",
        "desc": "Complete 10 trades",
        "category": "trade",
        "event": "trade",
        "threshold": 10,
        "reward": 500,
    },
    "trade_50": {
        "name": "Trade Mogul",
        "desc": "Complete 50 trades",
        "category": "trade",
        "event": "trade",
        "threshold": 50,
        "reward": 2000,
    },
    # --- Daily streak ---
    "streak_3": {
        "name": "Consistent",
        "desc": "3-day daily streak",
        "category": "streak",
        "event": "daily",
        "threshold": 3,
        "reward": 300,
    },
    "streak_7": {
        "name": "Dedicated",
        "desc": "7-day daily streak",
        "category": "streak",
        "event": "daily",
        "threshold": 7,
        "reward": 700,
    },
    "streak_14": {
        "name": "Committed",
        "desc": "14-day daily streak",
        "category": "streak",
        "event": "daily",
        "threshold": 14,
        "reward": 1500,
    },
    "streak_30": {
        "name": "Unstoppable",
        "desc": "30-day daily streak",
        "category": "streak",
        "event": "daily",
        "threshold": 30,
        "reward": 5000,
    },
    # --- Special (one-time conditions) ---
    "perfect_iv": {
        "name": "Perfection",
        "desc": "Catch a 100% IV Pokemon",
        "category": "special",
        "event": "catch_perfect",
        "threshold": 1,
        "reward": 1000,
    },
    "legendary_catch": {
        "name": "Legend Tamer",
        "desc": "Catch a legendary Pokemon",
        "category": "special",
        "event": "catch_legendary",
        "threshold": 1,
        "reward": 500,
    },
    "mythical_catch": {
        "name": "Myth Seeker",
        "desc": "Catch a mythical Pokemon",
        "category": "special",
        "event": "catch_mythical",
        "threshold": 1,
        "reward": 1000,
    },
    # --- Wonder Trade ---
    "wonder_10": {
        "name": "Wonder Trader",
        "desc": "Complete 10 Wonder Trades",
        "category": "wonder",
        "event": "wonder_trade",
        "threshold": 10,
        "reward": 500,
    },
}

# Build a quick lookup: event -> list of (achievement_id, achievement_def)
_EVENT_MAP: dict[str, list[tuple[str, dict]]] = {}
for _aid, _ach in ACHIEVEMENTS.items():
    _EVENT_MAP.setdefault(_ach["event"], []).append((_aid, _ach))


# ---------------------------------------------------------------------------
# Count helpers
# ---------------------------------------------------------------------------

async def _get_event_count(
    session: AsyncSession, user_id: int, event: str
) -> int:
    """Get the current count relevant to an event type."""
    if event == "catch":
        r = await session.execute(
            select(func.count(Pokemon.id)).where(Pokemon.owner_id == user_id)
        )
        return r.scalar() or 0

    if event == "catch_shiny":
        r = await session.execute(
            select(func.count(Pokemon.id)).where(
                Pokemon.owner_id == user_id, Pokemon.is_shiny.is_(True)
            )
        )
        return r.scalar() or 0

    if event in ("catch_perfect", "catch_legendary", "catch_mythical"):
        # The caller only fires the event when the condition is true,
        # so count is always >= 1.
        return 1

    if event == "pokedex_update":
        r = await session.execute(
            select(func.count(PokedexEntry.species_id)).where(
                PokedexEntry.user_id == user_id
            )
        )
        return r.scalar() or 0

    if event == "evolve":
        r = await session.execute(
            select(User.total_evolutions).where(User.telegram_id == user_id)
        )
        return r.scalar() or 0

    if event == "battle_win":
        r = await session.execute(
            select(User.battle_wins).where(User.telegram_id == user_id)
        )
        return r.scalar() or 0

    if event == "trade":
        r = await session.execute(
            select(User.total_trades).where(User.telegram_id == user_id)
        )
        return r.scalar() or 0

    if event == "daily":
        r = await session.execute(
            select(User.daily_streak).where(User.telegram_id == user_id)
        )
        return r.scalar() or 0

    if event == "wonder_trade":
        r = await session.execute(
            select(func.count(WonderTrade.id)).where(
                WonderTrade.user_id == user_id,
                WonderTrade.is_matched.is_(True),
            )
        )
        return r.scalar() or 0

    return 0


# ---------------------------------------------------------------------------
# Main check function — called from handlers after an event
# ---------------------------------------------------------------------------

async def check_achievements(
    session: AsyncSession,
    user_id: int,
    event: str,
) -> list[dict[str, Any]]:
    """Check and unlock achievements for *event*.

    Returns a list of newly-unlocked achievement dicts (with "id" key added).
    The caller is responsible for committing afterwards if desired.
    TC rewards are added to the user balance automatically.
    """
    candidates = _EVENT_MAP.get(event)
    if not candidates:
        return []

    # Already-unlocked achievement IDs for this user
    result = await session.execute(
        select(UserAchievement.achievement_id).where(
            UserAchievement.user_id == user_id
        )
    )
    unlocked = set(result.scalars().all())

    # Filter to those not yet unlocked
    to_check = [(aid, ach) for aid, ach in candidates if aid not in unlocked]
    if not to_check:
        return []

    count = await _get_event_count(session, user_id, event)

    newly_unlocked: list[dict[str, Any]] = []
    total_reward = 0

    for aid, ach in to_check:
        if count >= ach["threshold"]:
            ua = UserAchievement(
                user_id=user_id,
                achievement_id=aid,
                unlocked_at=datetime.utcnow(),
            )
            session.add(ua)
            total_reward += ach["reward"]
            newly_unlocked.append({**ach, "id": aid})

    if newly_unlocked and total_reward > 0:
        user_result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user_obj = user_result.scalar_one()
        user_obj.balance += total_reward

    for ach in newly_unlocked:
        logger.info(
            "Achievement unlocked",
            user_id=user_id,
            achievement=ach["id"],
            reward=ach["reward"],
        )

    return newly_unlocked


def format_achievement_notification(achievements: list[dict]) -> str:
    """Format a short notification string for newly unlocked achievements."""
    if not achievements:
        return ""
    lines = []
    for ach in achievements:
        lines.append(
            f"\n<b>Achievement Unlocked: {ach['name']}</b>\n"
            f"{ach['desc']} — +{ach['reward']:,} TC"
        )
    return "\n".join(lines)
