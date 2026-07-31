"""Breeding system — compatibility, IV inheritance, egg creation, hatching."""

from __future__ import annotations

import random
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.core import shiny
from telemon.core.constants import NATURES, MAX_IV, determine_gender
from telemon.database.models import InventoryItem, Pokemon, PokemonSpecies
from telemon.database.models.breeding import DaycareSlot, PokemonEgg
from telemon.logging import get_logger
from telemon.core.text import esc

logger = get_logger(__name__)

DITTO_ID = 132
MAX_EGGS = 6
STAT_NAMES = ["hp", "attack", "defense", "sp_attack", "sp_defense", "speed"]

# Oval Charm (item 302): divides the number of steps an egg needs to hatch.
OVAL_CHARM_ID = 302
OVAL_CHARM_STEP_DIVISOR = 2
MIN_EGG_STEPS = 25

# Baby Pokemon that need special incense to breed (simplified: we don't require
# incense, the egg will always be the base-form baby).
# This list is informational only; the walk-back logic handles it.


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------

def check_compatibility(
    species1: PokemonSpecies,
    species2: PokemonSpecies,
    gender1: str | None,
    gender2: str | None,
) -> tuple[bool, str]:
    """Check if two Pokemon can breed.

    Returns (can_breed, reason_string).
    """
    egg_groups1 = [g.lower() for g in (species1.egg_groups or [])]
    egg_groups2 = [g.lower() for g in (species2.egg_groups or [])]

    is_ditto1 = species1.national_dex == DITTO_ID
    is_ditto2 = species2.national_dex == DITTO_ID

    # Two Dittos cannot breed
    if is_ditto1 and is_ditto2:
        return False, "Two Ditto cannot breed with each other."

    # Undiscovered group blocks breeding (legendaries, babies, etc.)
    if "no-eggs" in egg_groups1 or "undiscoverable" in egg_groups1:
        return False, f"{species1.name} cannot breed (Undiscovered egg group)."
    if "no-eggs" in egg_groups2 or "undiscoverable" in egg_groups2:
        return False, f"{species2.name} cannot breed (Undiscovered egg group)."

    # Ditto breeds with anything that isn't undiscovered
    if is_ditto1 or is_ditto2:
        return True, "Compatible (Ditto)."

    # Need male + female (or genderless + Ditto, already handled)
    genderless1 = gender1 is None
    genderless2 = gender2 is None

    if genderless1 or genderless2:
        return False, "Genderless Pokemon can only breed with Ditto."

    if gender1 == gender2:
        return False, "Need one male and one female Pokemon."

    # Check egg group overlap
    overlap = set(egg_groups1) & set(egg_groups2)
    if not overlap:
        return False, (
            f"{species1.name} ({', '.join(egg_groups1)}) and "
            f"{species2.name} ({', '.join(egg_groups2)}) share no egg groups."
        )

    return True, "Compatible!"


# ---------------------------------------------------------------------------
# Base species (lowest evolution in line)
# ---------------------------------------------------------------------------

async def get_base_species(
    session: AsyncSession, species: PokemonSpecies
) -> PokemonSpecies:
    """Walk evolves_from_species_id back to the base form."""
    current = species
    seen = {current.national_dex}

    while current.evolves_from_species_id is not None:
        parent_id = current.evolves_from_species_id
        if parent_id in seen:
            break  # Prevent infinite loops
        seen.add(parent_id)

        result = await session.execute(
            select(PokemonSpecies).where(PokemonSpecies.national_dex == parent_id)
        )
        parent = result.scalar_one_or_none()
        if parent is None:
            break
        current = parent

    return current


async def determine_egg_species(
    session: AsyncSession,
    parent1: Pokemon,
    parent2: Pokemon,
) -> PokemonSpecies:
    """Determine which species the egg will be.

    The egg is the base form of the mother's line.
    If one parent is Ditto, the egg is the base form of the non-Ditto parent.
    """
    p1_is_ditto = parent1.species_id == DITTO_ID
    p2_is_ditto = parent2.species_id == DITTO_ID

    if p1_is_ditto and not p2_is_ditto:
        mother = parent2
    elif p2_is_ditto and not p1_is_ditto:
        mother = parent1
    else:
        # Neither is Ditto — use the female
        if parent1.gender == "female":
            mother = parent1
        else:
            mother = parent2

    # Ensure species relationship is loaded
    if mother.species is None:
        result = await session.execute(
            select(PokemonSpecies).where(
                PokemonSpecies.national_dex == mother.species_id
            )
        )
        mother_species = result.scalar_one()
    else:
        mother_species = mother.species

    return await get_base_species(session, mother_species)


# ---------------------------------------------------------------------------
# IV inheritance
# ---------------------------------------------------------------------------

def calculate_inherited_ivs(
    parent1: Pokemon, parent2: Pokemon
) -> dict[str, int]:
    """Calculate IVs for offspring.

    3 random stats inherited from a random parent, rest random 0-31.
    """
    inherited_stats = random.sample(STAT_NAMES, 3)
    ivs: dict[str, int] = {}

    for stat in STAT_NAMES:
        if stat in inherited_stats:
            # Pick from a random parent
            donor = random.choice([parent1, parent2])
            ivs[stat] = getattr(donor, f"iv_{stat}")
        else:
            ivs[stat] = random.randint(0, MAX_IV)

    return ivs


# ---------------------------------------------------------------------------
# Egg creation
# ---------------------------------------------------------------------------

async def has_oval_charm(session: AsyncSession, user_id: int) -> bool:
    """Whether the user owns an Oval Charm (item 302)."""
    result = await session.execute(
        select(InventoryItem)
        .where(InventoryItem.user_id == user_id)
        .where(InventoryItem.item_id == OVAL_CHARM_ID)
        .where(InventoryItem.quantity > 0)
    )
    return result.scalar_one_or_none() is not None


async def create_egg(
    session: AsyncSession,
    user_id: int,
    parent1: Pokemon,
    parent2: Pokemon,
) -> PokemonEgg | None:
    """Create an egg from two parents in daycare.

    Returns the egg, or None if the user has too many eggs.
    """
    # Check egg limit
    count_result = await session.execute(
        select(func.count()).select_from(PokemonEgg).where(
            PokemonEgg.user_id == user_id
        )
    )
    egg_count = count_result.scalar() or 0
    if egg_count >= MAX_EGGS:
        return None

    # Determine species
    egg_species = await determine_egg_species(session, parent1, parent2)

    # Calculate IVs
    ivs = calculate_inherited_ivs(parent1, parent2)

    # Steps from hatch_counter (scale down for messaging pace).  The Oval
    # Charm halves the requirement -- that is its whole effect.
    steps_total = max(egg_species.hatch_counter * 10, 50)
    oval_charm = await has_oval_charm(session, user_id)
    if oval_charm:
        steps_total = max(steps_total // OVAL_CHARM_STEP_DIVISOR, MIN_EGG_STEPS)

    # Shiny chance -- shares the Shiny Charm with wild hunting, so the odds a
    # player sees advertised on the charm hold for eggs too.  Chain is 0: a
    # shiny hunt tracks one wild species and has no bearing on breeding.
    rate = shiny.shiny_rate(0, await shiny.has_shiny_charm(session, user_id))
    is_shiny = random.randint(1, rate) == 1

    egg = PokemonEgg(
        id=uuid.uuid4(),
        user_id=user_id,
        species_id=egg_species.national_dex,
        parent1_id=parent1.id,
        parent2_id=parent2.id,
        iv_hp=ivs["hp"],
        iv_attack=ivs["attack"],
        iv_defense=ivs["defense"],
        iv_sp_attack=ivs["sp_attack"],
        iv_sp_defense=ivs["sp_defense"],
        iv_speed=ivs["speed"],
        steps_remaining=steps_total,
        steps_total=steps_total,
        is_shiny=is_shiny,
    )

    session.add(egg)
    await session.flush()

    logger.info(
        "Egg created",
        user_id=user_id,
        egg_species=egg_species.name,
        steps=steps_total,
        is_shiny=is_shiny,
        oval_charm=oval_charm,
    )
    return egg


# ---------------------------------------------------------------------------
# Hatching
# ---------------------------------------------------------------------------

async def hatch_egg(
    session: AsyncSession, egg: PokemonEgg
) -> Pokemon:
    """Hatch an egg into a new Pokemon.

    The egg must have steps_remaining <= 0.
    """
    from telemon.core.moves import assign_starter_moves

    # Get species
    species = egg.species
    if species is None:
        result = await session.execute(
            select(PokemonSpecies).where(
                PokemonSpecies.national_dex == egg.species_id
            )
        )
        species = result.scalar_one()

    # Determine gender
    gender = _determine_gender(species)

    # Determine ability
    ability = None
    if species.abilities:
        ability = random.choice(species.abilities)

    # Determine nature
    nature = random.choice(NATURES)

    pokemon = Pokemon(
        id=uuid.uuid4(),
        owner_id=egg.user_id,
        species_id=egg.species_id,
        nickname=None,
        level=1,
        experience=0,
        iv_hp=egg.iv_hp,
        iv_attack=egg.iv_attack,
        iv_defense=egg.iv_defense,
        iv_sp_attack=egg.iv_sp_attack,
        iv_sp_defense=egg.iv_sp_defense,
        iv_speed=egg.iv_speed,
        nature=nature,
        ability=ability,
        is_shiny=egg.is_shiny,
        gender=gender,
        friendship=120,  # Hatched Pokemon start with higher friendship
        original_trainer_id=egg.user_id,
        caught_at=datetime.utcnow(),
    )

    session.add(pokemon)
    # Need species loaded for assign_starter_moves
    pokemon.species = species
    await assign_starter_moves(session, pokemon)

    # Delete the egg
    await session.delete(egg)
    await session.flush()

    logger.info(
        "Egg hatched",
        user_id=egg.user_id,
        species=species.name,
        is_shiny=egg.is_shiny,
        iv_pct=pokemon.iv_percentage,
    )

    return pokemon


# ---------------------------------------------------------------------------
# Step tracking
# ---------------------------------------------------------------------------

async def add_steps_to_eggs(
    session: AsyncSession, user_id: int, steps: int = 1
) -> list[PokemonEgg]:
    """Decrement steps on all eggs for a user.

    Returns list of eggs that are now ready to hatch (steps_remaining <= 0).
    """
    result = await session.execute(
        select(PokemonEgg)
        .where(PokemonEgg.user_id == user_id)
        .where(PokemonEgg.steps_remaining > 0)
    )
    eggs = list(result.scalars().all())

    ready = []
    for egg in eggs:
        egg.steps_remaining = max(0, egg.steps_remaining - steps)
        if egg.steps_remaining <= 0:
            ready.append(egg)

    return ready


# ---------------------------------------------------------------------------
# Daycare helpers
# ---------------------------------------------------------------------------

async def get_daycare_slots(
    session: AsyncSession, user_id: int
) -> list[DaycareSlot]:
    """Get all daycare slots for a user (0-2)."""
    result = await session.execute(
        select(DaycareSlot)
        .where(DaycareSlot.user_id == user_id)
        .order_by(DaycareSlot.slot)
    )
    return list(result.scalars().all())


async def add_to_daycare(
    session: AsyncSession, user_id: int, pokemon: Pokemon
) -> tuple[bool, str]:
    """Add a Pokemon to daycare. Returns (success, message)."""
    slots = await get_daycare_slots(session, user_id)

    if len(slots) >= 2:
        return False, "Daycare is full! Remove a Pokemon first."

    # Check if this Pokemon is already in daycare
    for slot in slots:
        if slot.pokemon_id == pokemon.id:
            return False, f"{esc(pokemon.display_name)} is already in the daycare!"

    # Check if Pokemon is available
    if pokemon.is_on_market:
        return False, "Can't put a Pokemon on the market into daycare."
    if pokemon.is_in_trade:
        return False, "Can't put a Pokemon in trade into daycare."

    next_slot = 1 if not slots else (2 if slots[0].slot == 1 else 1)

    daycare_slot = DaycareSlot(
        user_id=user_id,
        pokemon_id=pokemon.id,
        slot=next_slot,
    )
    session.add(daycare_slot)
    await session.flush()

    return True, f"{esc(pokemon.display_name)} placed in daycare slot {next_slot}!"


async def remove_from_daycare(
    session: AsyncSession, user_id: int, slot_num: int
) -> tuple[bool, str]:
    """Remove a Pokemon from a daycare slot. Returns (success, message)."""
    result = await session.execute(
        select(DaycareSlot)
        .where(DaycareSlot.user_id == user_id)
        .where(DaycareSlot.slot == slot_num)
    )
    slot = result.scalar_one_or_none()

    if not slot:
        return False, f"No Pokemon in daycare slot {slot_num}."

    pokemon = slot.pokemon
    name = esc(pokemon.display_name) if pokemon else f"Pokemon (slot {slot_num})"

    await session.delete(slot)
    await session.flush()

    return True, f"{name} removed from daycare!"


async def get_user_eggs(
    session: AsyncSession, user_id: int
) -> list[PokemonEgg]:
    """Get all eggs for a user."""
    result = await session.execute(
        select(PokemonEgg)
        .where(PokemonEgg.user_id == user_id)
        .order_by(PokemonEgg.steps_remaining)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_determine_gender = determine_gender
