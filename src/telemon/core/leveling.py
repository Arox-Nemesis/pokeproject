"""XP and leveling system with improved level curve."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.core.constants import MAX_LEVEL
from telemon.logging import get_logger

logger = get_logger(__name__)


def apply_xp_boost(xp: int, boost_until: datetime | None) -> int:
    """Double XP if the user has an active XP Boost."""
    if boost_until and boost_until > datetime.utcnow():
        return xp * 2
    return xp


def xp_for_next_level(level: int) -> int:
    """Calculate XP needed to advance from current level to next.

    Uses a balanced polynomial curve:
    - L1: ~51 XP    (fast early leveling)
    - L10: ~626 XP
    - L20: ~1,568 XP
    - L30: ~2,873 XP
    - L50: ~6,493 XP
    - L70: ~11,538 XP
    - L100: ~21,944 XP (slow endgame)
    """
    return int(level ** 2.2 * 0.8 + level * 50)


async def add_xp_to_pokemon(
    session: AsyncSession, pokemon_id: str, xp_amount: int
) -> tuple[int, list[int], list[str]]:
    """Add XP to a Pokemon and handle level ups + move learning.

    Args:
        session: DB session
        pokemon_id: UUID string of the Pokemon
        xp_amount: Amount of XP to add

    Returns:
        Tuple of (xp_amount_actually_added, list_of_new_levels_reached, list_of_learned_move_names)
    """
    from telemon.database.models import Pokemon

    result = await session.execute(
        select(Pokemon).where(Pokemon.id == pokemon_id)
    )
    pokemon = result.scalar_one_or_none()

    if not pokemon or pokemon.level >= MAX_LEVEL:
        return 0, [], []

    old_level = pokemon.level
    pokemon.experience += xp_amount
    levels_gained = []

    xp_needed = xp_for_next_level(pokemon.level)
    while pokemon.experience >= xp_needed and pokemon.level < MAX_LEVEL:
        pokemon.experience -= xp_needed
        pokemon.level += 1
        levels_gained.append(pokemon.level)
        xp_needed = xp_for_next_level(pokemon.level)

    # Auto-learn moves on level-up
    learned_moves: list[str] = []
    if levels_gained:
        from telemon.core.moves import auto_learn_moves_on_levelup

        learned_moves = await auto_learn_moves_on_levelup(
            session, pokemon, old_level, pokemon.level
        )

    return xp_amount, levels_gained, learned_moves


def calculate_catch_xp(pokemon_level: int, catch_rate: int) -> int:
    """Calculate XP gained from catching a Pokemon.

    Rarer Pokemon give more XP.
    """
    base = 25 + pokemon_level * 2

    if catch_rate <= 3:
        base = int(base * 3.0)  # Ultra rare / legendary
    elif catch_rate <= 45:
        base = int(base * 2.0)  # Rare
    elif catch_rate <= 120:
        base = int(base * 1.5)  # Uncommon

    return base


def calculate_wild_battle_xp(player_level: int, wild_level: int) -> int:
    """Calculate XP gained from winning a wild battle."""
    base = 40 + wild_level * 5

    # Bonus for fighting higher level
    level_diff = wild_level - player_level
    if level_diff > 0:
        base = int(base * (1 + level_diff * 0.1))

    return base


def calculate_npc_battle_xp(player_level: int, npc_level: int, multiplier: float) -> int:
    """Calculate XP gained from beating an NPC trainer."""
    base = calculate_wild_battle_xp(player_level, npc_level)
    return int(base * multiplier)


def calculate_trade_xp() -> int:
    """XP gained from completing a trade."""
    return 50


def calculate_daily_xp(streak: int) -> int:
    """XP gained from daily claim, scales with streak."""
    return 20 + min(streak, 30) * 2


def format_xp_message(
    pokemon_name: str, xp_amount: int, levels_gained: list[int],
    learned_moves: list[str] | None = None,
) -> str:
    """Format an XP gain message for display."""
    parts = [f"{pokemon_name}: +{xp_amount} XP"]

    if levels_gained:
        new_level = levels_gained[-1]
        if len(levels_gained) == 1:
            parts.append(f"Level up! Now Lv.{new_level}!")
        else:
            parts.append(f"Gained {len(levels_gained)} levels! Now Lv.{new_level}!")

    if learned_moves:
        for move in learned_moves:
            parts.append(f"Learned {move}!")

    return " | ".join(parts)
