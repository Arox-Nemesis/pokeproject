"""Centralized game constants for PokeVault.

All magic numbers, game rules, and shared constants live here.
Import from this module instead of hardcoding values in handlers.
"""

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telemon.database.models import PokemonSpecies

# ------------------------------------------------------------------ #
# Pokemon Stats
# ------------------------------------------------------------------ #
MAX_LEVEL: int = 100
MAX_IV: int = 31
MAX_IV_TOTAL: int = 6 * MAX_IV  # 186
STARTER_IV_FLOOR: int = 10
STARTER_LEVEL: int = 5
CATCH_LEVEL_MIN: int = 1
CATCH_LEVEL_MAX: int = 30

# ------------------------------------------------------------------ #
# Friendship
# ------------------------------------------------------------------ #
MAX_FRIENDSHIP: int = 255
BASE_FRIENDSHIP: int = 70

# ------------------------------------------------------------------ #
# Species / Generations
# ------------------------------------------------------------------ #
MAX_GENERATION: int = 9
TOTAL_SPECIES: int = 1025

# ------------------------------------------------------------------ #
# Economy
# ------------------------------------------------------------------ #
MAX_GIFT_AMOUNT: int = 1_000_000
MARKET_MIN_PRICE: int = 100
MARKET_MAX_PRICE: int = 1_000_000_000
MARKET_LISTING_DAYS: int = 7
WT_COOLDOWN_SECONDS: int = 300
BREED_COOLDOWN_SECONDS: int = 1800

# ------------------------------------------------------------------ #
# Natures (25 canonical Pokemon natures, sorted)
# ------------------------------------------------------------------ #
NATURES: list[str] = [
    "adamant", "bashful", "bold", "brave", "calm",
    "careful", "docile", "gentle", "hardy", "hasty",
    "impish", "jolly", "lax", "lonely", "mild",
    "modest", "naive", "naughty", "quiet", "quirky",
    "rash", "relaxed", "sassy", "serious", "timid",
]

# ------------------------------------------------------------------ #
# Types (18 canonical Pokemon types)
# ------------------------------------------------------------------ #
VALID_TYPES: set[str] = {
    "normal", "fire", "water", "grass", "electric", "ice",
    "fighting", "poison", "ground", "flying", "psychic", "bug",
    "rock", "ghost", "dragon", "dark", "steel", "fairy",
}

# ------------------------------------------------------------------ #
# Rarity keywords for spawn filters
# ------------------------------------------------------------------ #
RARITY_KEYWORDS: set[str] = {
    "legendary", "mythical", "rare", "ultra_rare", "uncommon", "common",
}


# ------------------------------------------------------------------ #
# Shared helpers
# ------------------------------------------------------------------ #

def determine_gender(species: "PokemonSpecies") -> str | None:
    """Determine gender based on species gender_ratio.

    gender_ratio = percentage chance of being female.
    None = genderless, 0 = always male, 100 = always female.
    """
    if species.gender_ratio is None:
        return None  # Genderless

    if species.gender_ratio >= 100:
        return "female"
    if species.gender_ratio <= 0:
        return "male"

    if random.random() * 100 < species.gender_ratio:
        return "female"
    return "male"


def iv_percentage(iv_total: int) -> float:
    """Calculate IV percentage from total IV sum."""
    return round((iv_total / MAX_IV_TOTAL) * 100, 1)


def random_nature() -> str:
    """Pick a random nature."""
    return random.choice(NATURES)
