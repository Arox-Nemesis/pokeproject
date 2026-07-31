"""Shiny odds: the single source of truth for chain and Shiny Charm bonuses.

The wild-spawn shiny roll happens when the spawn is created, before anyone has
typed ``/catch``, so it cannot know whose chain or charm applies.  Rather than
defer the roll (which would let two people see different sprites for the same
spawn), the catcher gets a second, *conditional* roll at catch time:

    P(shiny for hunter) = P(rolled at spawn) + P(not rolled) * p_extra

Solving for ``p_extra`` with the target rate gives ``bonus_probability`` below,
so a hunter's end-to-end odds are exactly ``1/shiny_rate(chain, charm)`` while
non-hunters keep the plain base rate.  ``shinyhunt`` renders its odds table from
the same table, so what the UI advertises is what the code rolls.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.config import settings
from telemon.database.models import InventoryItem

# (minimum chain, denominator divisor).  Strictly increasing in both columns.
CHAIN_TIERS: tuple[tuple[int, float], ...] = (
    (0, 1.0),
    (10, 1.5),
    (25, 2.0),
    (50, 2.5),
    (75, 3.0),
    (100, 4.0),
    (150, 6.0),
    (200, 8.0),
)

# Shiny Charm (item 301) divides the denominator by this on top of the chain.
SHINY_CHARM_DIVISOR = 3.0

# Item id of the Shiny Charm in core.items.
SHINY_CHARM_ID = 301


async def has_shiny_charm(session: AsyncSession, user_id: int) -> bool:
    """Whether the user owns a Shiny Charm."""
    result = await session.execute(
        select(InventoryItem)
        .where(InventoryItem.user_id == user_id)
        .where(InventoryItem.item_id == SHINY_CHARM_ID)
        .where(InventoryItem.quantity > 0)
    )
    return result.scalar_one_or_none() is not None


def chain_divisor(chain: int) -> float:
    """Return the denominator divisor earned by a chain of ``chain`` catches."""
    divisor = 1.0
    for threshold, value in CHAIN_TIERS:
        if chain >= threshold:
            divisor = value
    return divisor


def shiny_rate(chain: int = 0, has_charm: bool = False) -> int:
    """Return the shiny denominator (``1/N``) for a chain + charm combination."""
    rate = settings.shiny_base_rate / chain_divisor(chain)
    if has_charm:
        rate /= SHINY_CHARM_DIVISOR
    return max(1, int(round(rate)))


def format_odds(chain: int = 0, has_charm: bool = False) -> str:
    """Human-readable odds string, e.g. ``"1/1365"``."""
    return f"1/{shiny_rate(chain, has_charm)}"


def bonus_probability(chain: int, has_charm: bool) -> float:
    """Extra chance to apply at catch time, given the spawn roll already failed.

    Returns 0.0 when the hunter has no bonus over the base rate.
    """
    base = settings.shiny_base_rate
    target = shiny_rate(chain, has_charm)
    if target >= base:
        return 0.0

    p_base = 1.0 / base
    p_target = 1.0 / target
    return (p_target - p_base) / (1.0 - p_base)
