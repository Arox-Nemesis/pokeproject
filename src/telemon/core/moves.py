"""Move management system — learning, forgetting, and looking up moves."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.core import forms, regional
from telemon.database.models import Pokemon
from telemon.database.models.move import Move, PokemonLearnset
from telemon.logging import get_logger
from telemon.core.text import esc

logger = get_logger(__name__)

MAX_MOVES = 4


def learnset_species_id(species_id: int) -> int:
    """Map a species id to the id its learnset rows are stored under.

    ``pokemon_learnsets`` only covers dex < 10000, so every alternate form
    (regional variants and Megas) has to fall back to its base species or it
    would know no moves at all and could never learn any.
    """
    if species_id < 10000:
        return species_id

    base = regional.get_base_dex(species_id)
    if base != species_id:
        return base

    mega = forms.get_base_species_for_mega_dex(species_id)
    if mega:
        return mega[0]

    logger.warning("No learnset fallback for alternate form", species_id=species_id)
    return species_id


async def get_move_by_name(session: AsyncSession, name: str) -> Move | None:
    """Look up a move by name (case-insensitive)."""
    result = await session.execute(
        select(Move).where(Move.name_lower == name.lower())
    )
    return result.scalar_one_or_none()


async def get_moves_by_names(session: AsyncSession, names: list[str]) -> list[Move]:
    """Look up multiple moves by name."""
    if not names:
        return []
    lower_names = [n.lower() for n in names]
    result = await session.execute(
        select(Move).where(Move.name_lower.in_(lower_names))
    )
    return list(result.scalars().all())


async def get_pokemon_known_moves(session: AsyncSession, pokemon: Pokemon) -> list[Move]:
    """Get the full Move objects for a Pokemon's known moves."""
    if not pokemon.moves:
        return []
    return await get_moves_by_names(session, pokemon.moves)


async def get_learnable_moves(
    session: AsyncSession, species_id: int, max_level: int
) -> list[dict]:
    """Get all moves a species can learn up to a given level.

    Returns list of dicts: {"move": Move, "level": int}
    """
    result = await session.execute(
        select(PokemonLearnset, Move)
        .join(Move, Move.id == PokemonLearnset.move_id)
        .where(PokemonLearnset.species_id == learnset_species_id(species_id))
        .where(PokemonLearnset.learn_method == "level-up")
        .where(PokemonLearnset.level_learned <= max_level)
        .order_by(PokemonLearnset.level_learned)
    )
    rows = result.all()
    return [{"move": move, "level": ls.level_learned} for ls, move in rows]


async def get_moves_at_level(
    session: AsyncSession, species_id: int, level: int
) -> list[Move]:
    """Get moves learned exactly at a specific level."""
    result = await session.execute(
        select(Move)
        .join(PokemonLearnset, PokemonLearnset.move_id == Move.id)
        .where(PokemonLearnset.species_id == learnset_species_id(species_id))
        .where(PokemonLearnset.learn_method == "level-up")
        .where(PokemonLearnset.level_learned == level)
    )
    return list(result.scalars().all())


async def auto_learn_moves_on_levelup(
    session: AsyncSession, pokemon: Pokemon, old_level: int, new_level: int
) -> list[str]:
    """Auto-learn any moves the Pokemon should know for its new level(s).

    If the Pokemon has < 4 moves, just add them.
    If it already has 4, skip (user must manually manage via /learn, /forget).

    Returns list of move names that were learned.
    """
    current_moves = list(pokemon.moves or [])
    learned = []

    for level in range(old_level + 1, new_level + 1):
        new_moves = await get_moves_at_level(session, pokemon.species_id, level)

        for move in new_moves:
            move_name_lower = move.name_lower

            # Skip if already known
            if move_name_lower in [m.lower() for m in current_moves]:
                continue

            if len(current_moves) < MAX_MOVES:
                current_moves.append(move_name_lower)
                learned.append(move.name)
            else:
                # Already has 4 moves — skip auto-learn
                # User can manually learn via /learn command
                pass

    if learned:
        pokemon.moves = current_moves
        # No commit here — caller handles it

    return learned


async def assign_starter_moves(
    session: AsyncSession, pokemon: Pokemon
) -> list[str]:
    """Assign initial moves to a Pokemon based on its level.

    Picks the best moves available up to the Pokemon's current level.
    Prioritizes: damaging moves with STAB, then damaging moves, then any.
    """
    learnable = await get_learnable_moves(session, pokemon.species_id, pokemon.level)

    if not learnable:
        return []

    # Get species types for STAB check
    species = pokemon.species
    species_types = [species.type1.lower()]
    if species.type2:
        species_types.append(species.type2.lower())

    # Score and sort moves
    def move_score(entry: dict) -> float:
        move = entry["move"]
        score = 0.0

        # Prefer damaging moves
        if move.power and move.power > 0:
            score += move.power

            # STAB bonus
            if move.type in species_types:
                score += 50

            # Accuracy bonus
            if move.accuracy and move.accuracy >= 90:
                score += 20
        else:
            # Status moves get low base score
            score += 10

        return score

    # Sort by score descending, pick top 4
    scored = sorted(learnable, key=move_score, reverse=True)
    best = scored[:MAX_MOVES]

    move_names = [entry["move"].name_lower for entry in best]
    pokemon.moves = move_names

    return [entry["move"].name for entry in best]


async def learn_move(
    session: AsyncSession, pokemon: Pokemon, move_name: str
) -> tuple[bool, str]:
    """Try to learn a specific move. Returns (success, message)."""
    # Check if move exists
    move = await get_move_by_name(session, move_name)
    if not move:
        return False, f"Move '{esc(move_name)}' not found."

    # Check if species can learn this move
    result = await session.execute(
        select(PokemonLearnset)
        .where(PokemonLearnset.species_id == learnset_species_id(pokemon.species_id))
        .where(PokemonLearnset.move_id == move.id)
    )
    learnset_entry = result.scalar_one_or_none()

    if not learnset_entry:
        return False, f"{esc(pokemon.display_name)} can't learn {move.name}!"

    # Check level requirement for level-up moves
    if learnset_entry.learn_method == "level-up" and learnset_entry.level_learned:
        if pokemon.level < learnset_entry.level_learned:
            return False, (
                f"{esc(pokemon.display_name)} needs to be Lv.{learnset_entry.level_learned} "
                f"to learn {move.name} (currently Lv.{pokemon.level})."
            )

    current_moves = list(pokemon.moves or [])

    # Already knows this move?
    if move.name_lower in [m.lower() for m in current_moves]:
        return False, f"{esc(pokemon.display_name)} already knows {move.name}!"

    # Has room?
    if len(current_moves) < MAX_MOVES:
        current_moves.append(move.name_lower)
        pokemon.moves = current_moves
        return True, f"{esc(pokemon.display_name)} learned {move.name}!"
    else:
        return False, (
            f"{esc(pokemon.display_name)} already knows {MAX_MOVES} moves!\n"
            f"Use /forget [move] first to make room."
        )


async def forget_move(
    session: AsyncSession, pokemon: Pokemon, move_name: str
) -> tuple[bool, str]:
    """Forget a move. Returns (success, message)."""
    current_moves = list(pokemon.moves or [])

    if not current_moves:
        return False, f"{esc(pokemon.display_name)} doesn't know any moves!"

    # Find the move (case-insensitive)
    target_lower = move_name.lower()
    found_idx = None
    found_name = None

    for i, m in enumerate(current_moves):
        if m.lower() == target_lower:
            found_idx = i
            found_name = m
            break

    if found_idx is None:
        # Try partial match
        for i, m in enumerate(current_moves):
            if target_lower in m.lower():
                found_idx = i
                found_name = m
                break

    if found_idx is None:
        known = ", ".join(current_moves)
        return False, f"{esc(pokemon.display_name)} doesn't know '{esc(move_name)}'.\nKnown moves: {known}"

    current_moves.pop(found_idx)
    pokemon.moves = current_moves

    return True, f"{esc(pokemon.display_name)} forgot {found_name}!"
