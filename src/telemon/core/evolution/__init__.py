"""Evolution system for Pokemon."""

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.core.items import ITEM_BY_NAME, LINKING_CORD_ID
from telemon.database.models import InventoryItem, Pokemon, PokemonSpecies
from telemon.logging import get_logger

logger = get_logger(__name__)

# Load evolution data
_EVOLUTION_DATA: dict[str, Any] = {}


def _load_evolution_data() -> dict[str, Any]:
    """Load evolution data from JSON file."""
    global _EVOLUTION_DATA
    if _EVOLUTION_DATA:
        return _EVOLUTION_DATA

    data_path = Path(__file__).parent.parent.parent.parent.parent / "data" / "evolutions.json"
    if data_path.exists():
        with open(data_path) as f:
            _EVOLUTION_DATA = json.load(f)
    else:
        logger.warning("Evolution data file not found", path=str(data_path))
        _EVOLUTION_DATA = {}

    return _EVOLUTION_DATA


def get_evolution_data() -> dict[str, Any]:
    """Get evolution data."""
    return _load_evolution_data()


class EvolutionResult:
    """Result of an evolution check or attempt."""

    def __init__(
        self,
        can_evolve: bool,
        evolved_species_id: int | None = None,
        evolved_species_name: str | None = None,
        trigger: str | None = None,
        requirement: str | None = None,
        missing_requirement: str | None = None,
    ):
        self.can_evolve = can_evolve
        self.evolved_species_id = evolved_species_id
        self.evolved_species_name = evolved_species_name
        self.trigger = trigger
        self.requirement = requirement
        self.missing_requirement = missing_requirement


async def check_evolution(
    session: AsyncSession,
    pokemon: Pokemon,
    user_id: int,
    use_item: str | None = None,
    is_trade: bool = False,
) -> EvolutionResult:
    """
    Check if a Pokemon can evolve.

    Args:
        session: Database session
        pokemon: The Pokemon to check
        user_id: The owner's user ID
        use_item: Item name if trying to evolve with an item
        is_trade: Whether this is a trade evolution check

    Returns:
        EvolutionResult with evolution details
    """
    evolution_data = get_evolution_data()
    species_id = pokemon.species_id

    # Special case: if the user is using a Linking Cord, treat as trade
    use_item_lower = use_item.lower().strip() if use_item else None
    using_linking_cord = use_item_lower == "linking cord"

    if using_linking_cord:
        is_trade = True
        # The linking cord doesn't act as a trade-item (e.g. metal coat),
        # it just triggers the trade itself. So clear use_item for the
        # trade-with-item check below and let it match "trade, item=none".
        use_item = None
        use_item_lower = None

    # Find evolution chain for this species
    possible_evolutions = []

    for chain_id, chain_data in evolution_data.items():
        for evo in chain_data.get("chain", []):
            if evo["species_id"] == species_id:
                possible_evolutions.append(evo)

    if not possible_evolutions:
        return EvolutionResult(
            can_evolve=False,
            missing_requirement="This Pokemon cannot evolve.",
        )

    # Check each possible evolution
    for evo in possible_evolutions:
        trigger = evo["trigger"]
        evolves_to = evo["evolves_to"]

        # Get the evolved species info
        result = await session.execute(
            select(PokemonSpecies).where(PokemonSpecies.national_dex == evolves_to)
        )
        evolved_species = result.scalar_one_or_none()

        if not evolved_species:
            continue

        if trigger == "level":
            min_level = evo.get("min_level", 1)
            if pokemon.level >= min_level:
                return EvolutionResult(
                    can_evolve=True,
                    evolved_species_id=evolves_to,
                    evolved_species_name=evolved_species.name,
                    trigger="level",
                    requirement=f"Level {min_level}+",
                )
            else:
                return EvolutionResult(
                    can_evolve=False,
                    evolved_species_id=evolves_to,
                    evolved_species_name=evolved_species.name,
                    trigger="level",
                    requirement=f"Level {min_level}+",
                    missing_requirement=f"Needs to reach level {min_level} (currently {pokemon.level})",
                )

        elif trigger == "item":
            required_item = evo.get("item", "").lower()

            if use_item_lower and use_item_lower == required_item:
                return EvolutionResult(
                    can_evolve=True,
                    evolved_species_id=evolves_to,
                    evolved_species_name=evolved_species.name,
                    trigger="item",
                    requirement=required_item.title(),
                )
            elif use_item_lower:
                # Wrong item — try next evolution
                continue
            else:
                # No item specified — tell user what's needed
                item_data = ITEM_BY_NAME.get(required_item)
                has_item = False
                if item_data:
                    inv_result = await session.execute(
                        select(InventoryItem)
                        .where(InventoryItem.user_id == user_id)
                        .where(InventoryItem.item_id == item_data["id"])
                        .where(InventoryItem.quantity > 0)
                    )
                    has_item = inv_result.scalar_one_or_none() is not None

                return EvolutionResult(
                    can_evolve=False,
                    evolved_species_id=evolves_to,
                    evolved_species_name=evolved_species.name,
                    trigger="item",
                    requirement=required_item.title(),
                    missing_requirement=f"Requires {required_item.title()}"
                    + (" (you have it! Use: /evolve [num] {})".format(required_item) if has_item else " (buy from /shop)"),
                )

        elif trigger == "trade":
            trade_item = evo.get("item")

            if is_trade:
                if trade_item and trade_item != "none":
                    # Trade evolution that also requires a held item
                    # When using Linking Cord, the user must also specify the item
                    if using_linking_cord:
                        # Check if user has the trade item
                        item_data = ITEM_BY_NAME.get(trade_item.lower())
                        has_item = False
                        if item_data:
                            inv_result = await session.execute(
                                select(InventoryItem)
                                .where(InventoryItem.user_id == user_id)
                                .where(InventoryItem.item_id == item_data["id"])
                                .where(InventoryItem.quantity > 0)
                            )
                            has_item = inv_result.scalar_one_or_none() is not None

                        if has_item:
                            return EvolutionResult(
                                can_evolve=True,
                                evolved_species_id=evolves_to,
                                evolved_species_name=evolved_species.name,
                                trigger="trade",
                                requirement=f"Trade + {trade_item.title()}",
                            )
                        else:
                            return EvolutionResult(
                                can_evolve=False,
                                evolved_species_id=evolves_to,
                                evolved_species_name=evolved_species.name,
                                trigger="trade",
                                requirement=f"Trade + {trade_item.title()}",
                                missing_requirement=f"Also requires {trade_item.title()} (buy from /shop)",
                            )
                    else:
                        # Real trade — check if the traded Pokemon holds the item
                        # For now, we let real trades evolve regardless of held item
                        return EvolutionResult(
                            can_evolve=True,
                            evolved_species_id=evolves_to,
                            evolved_species_name=evolved_species.name,
                            trigger="trade",
                            requirement=f"Trade + {trade_item.title()}",
                        )
                else:
                    # Simple trade evolution (no item needed)
                    return EvolutionResult(
                        can_evolve=True,
                        evolved_species_id=evolves_to,
                        evolved_species_name=evolved_species.name,
                        trigger="trade",
                        requirement="Trade",
                    )
            else:
                # Not trading — show requirement
                req = "Trade"
                if trade_item and trade_item != "none":
                    req += f" + {trade_item.title()}"

                # Check if user has a Linking Cord
                inv_result = await session.execute(
                    select(InventoryItem)
                    .where(InventoryItem.user_id == user_id)
                    .where(InventoryItem.item_id == LINKING_CORD_ID)
                    .where(InventoryItem.quantity > 0)
                )
                has_cord = inv_result.scalar_one_or_none() is not None

                hint = "Trade with another trainer"
                if has_cord:
                    hint += " or use: /evolve [num] linking cord"
                else:
                    hint += " or buy a Linking Cord from /shop"

                return EvolutionResult(
                    can_evolve=False,
                    evolved_species_id=evolves_to,
                    evolved_species_name=evolved_species.name,
                    trigger="trade",
                    requirement=req,
                    missing_requirement=hint,
                )

        elif trigger == "friendship":
            min_friendship = evo.get("min_friendship", 220)
            if pokemon.friendship >= min_friendship:
                return EvolutionResult(
                    can_evolve=True,
                    evolved_species_id=evolves_to,
                    evolved_species_name=evolved_species.name,
                    trigger="friendship",
                    requirement=f"Friendship {min_friendship}+",
                )
            else:
                return EvolutionResult(
                    can_evolve=False,
                    evolved_species_id=evolves_to,
                    evolved_species_name=evolved_species.name,
                    trigger="friendship",
                    requirement=f"Friendship {min_friendship}+",
                    missing_requirement=f"Needs {min_friendship} friendship (currently {pokemon.friendship}). Use /pet to increase!",
                )

    return EvolutionResult(
        can_evolve=False,
        missing_requirement="Evolution conditions not met.",
    )


async def evolve_pokemon(
    session: AsyncSession,
    pokemon: Pokemon,
    user_id: int,
    use_item: str | None = None,
    is_trade: bool = False,
) -> tuple[bool, str]:
    """
    Attempt to evolve a Pokemon.

    Args:
        session: Database session
        pokemon: The Pokemon to evolve
        user_id: The owner's user ID
        use_item: Item name if evolving with an item
        is_trade: Whether this is a trade evolution

    Returns:
        Tuple of (success, message)
    """
    use_item_lower = use_item.lower().strip() if use_item else None
    using_linking_cord = use_item_lower == "linking cord"

    # Check if can evolve
    result = await check_evolution(session, pokemon, user_id, use_item, is_trade)

    if not result.can_evolve:
        return False, result.missing_requirement or "Cannot evolve."

    # Get the evolved species
    species_result = await session.execute(
        select(PokemonSpecies).where(
            PokemonSpecies.national_dex == result.evolved_species_id
        )
    )
    evolved_species = species_result.scalar_one_or_none()

    if not evolved_species:
        return False, "Evolution target species not found."

    old_species_name = pokemon.species.name

    # Consume the item if used
    if result.trigger == "item" and use_item:
        item_data = ITEM_BY_NAME.get(use_item.lower().strip())
        if item_data:
            inv_result = await session.execute(
                select(InventoryItem)
                .where(InventoryItem.user_id == user_id)
                .where(InventoryItem.item_id == item_data["id"])
                .where(InventoryItem.quantity > 0)
            )
            inventory_item = inv_result.scalar_one_or_none()
            if inventory_item:
                inventory_item.quantity -= 1
            else:
                return False, f"You don't have a {use_item.title()}!"

    # If Linking Cord was used, consume it
    if using_linking_cord:
        inv_result = await session.execute(
            select(InventoryItem)
            .where(InventoryItem.user_id == user_id)
            .where(InventoryItem.item_id == LINKING_CORD_ID)
            .where(InventoryItem.quantity > 0)
        )
        cord_item = inv_result.scalar_one_or_none()
        if cord_item:
            cord_item.quantity -= 1
        else:
            return False, "You don't have a Linking Cord!"

        # If the trade evolution also needs an item, consume that too
        evolution_data = get_evolution_data()
        for chain_id, chain_data in evolution_data.items():
            for evo in chain_data.get("chain", []):
                if evo["species_id"] == pokemon.species_id and evo["evolves_to"] == result.evolved_species_id:
                    trade_item = evo.get("item")
                    if trade_item and trade_item != "none":
                        item_data = ITEM_BY_NAME.get(trade_item.lower())
                        if item_data:
                            inv_result = await session.execute(
                                select(InventoryItem)
                                .where(InventoryItem.user_id == user_id)
                                .where(InventoryItem.item_id == item_data["id"])
                                .where(InventoryItem.quantity > 0)
                            )
                            trade_inv = inv_result.scalar_one_or_none()
                            if trade_inv:
                                trade_inv.quantity -= 1
                            else:
                                return False, f"You don't have a {trade_item.title()}!"
                    break

    # Evolve the Pokemon
    pokemon.species_id = result.evolved_species_id

    # Pick new ability from evolved species
    import random
    if evolved_species.abilities:
        pokemon.ability = random.choice(evolved_species.abilities)

    await session.commit()

    logger.info(
        "Pokemon evolved",
        pokemon_id=str(pokemon.id),
        from_species=old_species_name,
        to_species=evolved_species.name,
        trigger=result.trigger,
    )

    return True, f"{old_species_name} evolved into {evolved_species.name}!"


def get_possible_evolutions(species_id: int) -> list[dict]:
    """Get all possible evolutions for a species."""
    evolution_data = get_evolution_data()
    evolutions = []

    for chain_id, chain_data in evolution_data.items():
        for evo in chain_data.get("chain", []):
            if evo["species_id"] == species_id:
                evolutions.append(evo)

    return evolutions
