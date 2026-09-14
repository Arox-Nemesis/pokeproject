"""Script to seed the database with Pokemon data from JSON files."""

import asyncio
import json
from pathlib import Path

from sqlalchemy import select

# Add parent to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from telemon.database import get_session_context, init_db
from telemon.database.models import Item, Move, PokemonSpecies, PokemonLearnset
from telemon.logging import setup_logging, get_logger

DATA_DIR = Path(__file__).parent.parent / "data"

logger = get_logger(__name__)


async def seed_pokemon(session) -> int:
    """Seed Pokemon species data."""
    pokemon_file = DATA_DIR / "pokemon.json"
    if not pokemon_file.exists():
        logger.warning("pokemon.json not found, skipping")
        return 0

    with open(pokemon_file) as f:
        pokemon_data = json.load(f)

    # BULK LOOKUP: Get all existing IDs at once
    existing = (await session.execute(select(PokemonSpecies.national_dex))).scalars().all()
    existing_set = set(existing)

    count = 0
    for poke in pokemon_data:
        if poke["national_dex"] in existing_set:
            continue

        species = PokemonSpecies(
            national_dex=poke["national_dex"],
            name=poke["name"],
            name_lower=poke["name_lower"],
            type1=poke["type1"],
            type2=poke.get("type2"),
            base_hp=poke["base_hp"],
            base_attack=poke["base_attack"],
            base_defense=poke["base_defense"],
            base_sp_attack=poke["base_sp_attack"],
            base_sp_defense=poke["base_sp_defense"],
            base_speed=poke["base_speed"],
            abilities=poke.get("abilities", []),
            hidden_ability=poke.get("hidden_ability"),
            catch_rate=poke.get("catch_rate", 45),
            base_friendship=poke.get("base_friendship", 70),
            base_experience=poke.get("base_experience", 64),
            growth_rate=poke.get("growth_rate", "medium"),
            gender_ratio=poke.get("gender_ratio"),
            egg_groups=poke.get("egg_groups", []),
            hatch_counter=poke.get("hatch_counter", 20),
            evolution_chain_id=poke.get("evolution_chain_id"),
            evolves_from_species_id=int(poke["evolves_from_species_id"]) if poke.get("evolves_from_species_id") else None,
            sprite_url=poke.get("sprite_url"),
            sprite_shiny_url=poke.get("sprite_shiny_url"),
            sprite_back_url=poke.get("sprite_back_url"),
            sprite_back_shiny_url=poke.get("sprite_back_shiny_url"),
            generation=poke.get("generation", 1),
            is_legendary=poke.get("is_legendary", False),
            is_mythical=poke.get("is_mythical", False),
            is_baby=poke.get("is_baby", False),
            height=poke.get("height", 10),
            weight=poke.get("weight", 100),
            flavor_text=poke.get("flavor_text"),
        )
        session.add(species)
        count += 1

    await session.commit()
    return count


async def seed_moves(session) -> int:
    """Seed moves data."""
    moves_file = DATA_DIR / "moves.json"
    if not moves_file.exists():
        logger.warning("moves.json not found, skipping")
        return 0

    with open(moves_file) as f:
        moves_data = json.load(f)

    # BULK LOOKUP
    existing = (await session.execute(select(Move.id))).scalars().all()
    existing_set = set(existing)

    count = 0
    for move_data in moves_data:
        if move_data["id"] in existing_set:
            continue

        move = Move(
            id=move_data["id"],
            name=move_data["name"],
            name_lower=move_data["name_lower"],
            type=move_data["type"],
            category=move_data["category"],
            power=move_data.get("power"),
            accuracy=move_data.get("accuracy"),
            pp=move_data.get("pp", 20),
            priority=move_data.get("priority", 0),
            effect=move_data.get("effect"),
            effect_chance=move_data.get("effect_chance"),
            target=move_data.get("target", "selected-pokemon"),
            generation=move_data.get("generation", 1),
            description=move_data.get("description"),
        )
        session.add(move)
        count += 1

    await session.commit()
    return count


async def seed_learnsets(session) -> int:
    """Seed Pokemon level-up learnsets."""
    learnsets_file = DATA_DIR / "learnsets.json"
    if not learnsets_file.exists():
        logger.warning("learnsets.json not found, skipping")
        return 0

    with open(learnsets_file) as f:
        learnsets_data = json.load(f)

    # BULK LOOKUP
    existing = (await session.execute(select(PokemonLearnset.species_id).distinct())).scalars().all()
    existing_set = set(existing)

    count = 0
    for entry in learnsets_data:
        species_id = entry["species_id"]
        if species_id in existing_set:
            continue

        for move_data in entry.get("level_up_moves", []):
            learnset = PokemonLearnset(
                species_id=species_id,
                move_id=move_data["move_id"],
                learn_method="level-up",          
                level_learned=move_data["level"]  
            )
            session.add(learnset)
            count += 1

    await session.commit()
    return count


async def seed_items(session) -> int:
    """Seed items data."""
    items_file = DATA_DIR / "items.json"
    if not items_file.exists():
        logger.warning("items.json not found, skipping")
        return 0

    with open(items_file) as f:
        items_data = json.load(f)

    # BULK LOOKUP
    existing = (await session.execute(select(Item.id))).scalars().all()
    existing_set = set(existing)

    count = 0
    for item_data in items_data:
        if item_data["id"] in existing_set:
            continue

        item = Item(
            id=item_data["id"],
            name=item_data["name"],
            name_lower=item_data["name_lower"],
            category=item_data["category"],
            cost=item_data.get("cost", 0),
            description=item_data.get("effect"),
            short_description=item_data.get("short_effect"),
            sprite_url=item_data.get("sprite_url"),
        )
        session.add(item)
        count += 1

    await session.commit()
    return count


async def main():
    """Seed all data into the database."""
    setup_logging()
    logger.info("Starting database seeding...")

    # Initialize database
    await init_db()

    async with get_session_context() as session:
        # Seed Pokemon
        pokemon_count = await seed_pokemon(session)
        logger.info(f"Seeded {pokemon_count} Pokemon species")

        # Seed Moves
        moves_count = await seed_moves(session)
        logger.info(f"Seeded {moves_count} moves")

        # Seed Learnsets
        learnsets_count = await seed_learnsets(session)
        logger.info(f"Seeded {learnsets_count} learnset entries")

        # Seed Items
        items_count = await seed_items(session)
        logger.info(f"Seeded {items_count} items")

    logger.info("Database seeding complete!")


if __name__ == "__main__":
    asyncio.run(main())
    
