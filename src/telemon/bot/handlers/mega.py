"""Mega Evolution commands — /mega and /demega."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.core.forms import (
    get_base_species_for_mega_dex,
    get_mega_forms_for_species,
    MegaForm,
)
from telemon.core.items import MEGA_STONE_BY_NAME
from telemon.database.models import InventoryItem, Pokemon, User
from telemon.logging import get_logger
from telemon.core.text import esc

router = Router(name="mega")
logger = get_logger(__name__)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

async def _resolve_pokemon(
    session: AsyncSession, user: User, idx_str: str | None,
) -> Pokemon | None:
    """Resolve a Pokémon by index, 'l'/'latest', or '0'/None for selected."""
    LATEST = {"l", "-l", "--latest", "-latest", "latest"}

    if idx_str is not None:
        low = idx_str.lower()
        if low in LATEST:
            result = await session.execute(
                select(Pokemon)
                .where(Pokemon.owner_id == user.telegram_id)
                .order_by(Pokemon.caught_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()
        if low == "0":
            pass  # fall through to selected
        elif low.isdigit():
            idx = int(low)
            poke_result = await session.execute(
                select(Pokemon)
                .where(Pokemon.owner_id == user.telegram_id)
                .order_by(Pokemon.caught_at.asc())
            )
            pokemon_list = list(poke_result.scalars().all())
            if 1 <= idx <= len(pokemon_list):
                return pokemon_list[idx - 1]
            return None

    # Selected Pokémon
    if user.selected_pokemon_id:
        sel = await session.execute(
            select(Pokemon)
            .where(Pokemon.id == user.selected_pokemon_id)
            .where(Pokemon.owner_id == user.telegram_id)
        )
        return sel.scalar_one_or_none()
    return None


def _find_stone_item_id(stone_name_lower: str) -> int | None:
    """Get the shop item ID for a mega stone by its name_lower."""
    entry = MEGA_STONE_BY_NAME.get(stone_name_lower)
    return entry["id"] if entry else None


# ──────────────────────────────────────────────
# /mega  — permanently mega evolve a Pokémon
# ──────────────────────────────────────────────

@router.message(Command("mega"))
async def cmd_mega(message: Message, session: AsyncSession, user: User) -> None:
    """Mega Evolve a Pokémon permanently.

    Usage:
        /mega           — mega evolve selected Pokémon
        /mega <id>      — mega evolve Pokémon #id
        /mega <id> x/y  — choose X or Y form (Charizard, Mewtwo)
    """
    if not message.from_user:
        return

    args = (message.text or "").split()[1:]  # strip /mega

    # Parse Pokémon index
    poke_idx = args[0] if args else None
    form_hint = args[1].lower() if len(args) >= 2 else None

    pokemon = await _resolve_pokemon(session, user, poke_idx)
    if not pokemon:
        await message.answer(
            "Couldn't find that Pokémon. "
            "Usage: <code>/mega [id]</code>"
        )
        return

    # Check if already mega
    base_check = get_base_species_for_mega_dex(pokemon.species_id)
    if base_check is not None:
        await message.answer(
            f"<b>{esc(pokemon.display_name)}</b> is already Mega Evolved!\n"
            f"Use <code>/demega</code> to revert first."
        )
        return

    # Get available mega forms for this species
    mega_forms = get_mega_forms_for_species(pokemon.species_id)
    if not mega_forms:
        await message.answer(
            f"<b>{esc(pokemon.display_name)}</b> cannot Mega Evolve!"
        )
        return

    # If multiple forms (Charizard X/Y, Mewtwo X/Y), require a hint
    chosen: MegaForm | None = None
    if len(mega_forms) == 1:
        chosen = mega_forms[0]
    else:
        if form_hint:
            for mf in mega_forms:
                if form_hint in mf.form_name.lower():
                    chosen = mf
                    break
        if not chosen:
            form_list = "\n".join(
                f"  • <b>{mf.form_name}</b> — requires {mf.mega_stone_display or 'Dragon Ascent'}"
                for mf in mega_forms
            )
            await message.answer(
                f"<b>{esc(pokemon.display_name)}</b> has multiple Mega forms:\n\n"
                f"{form_list}\n\n"
                f"Use <code>/mega {poke_idx or ''} x</code> or "
                f"<code>/mega {poke_idx or ''} y</code> to choose."
            )
            return

    # Rayquaza special case (no stone, requires Dragon Ascent)
    if chosen.mega_stone is None:
        # Rayquaza — check for Dragon Ascent in moves
        move_names = [m.lower() for m in (pokemon.moves or [])]
        if "dragon ascent" not in move_names:
            await message.answer(
                f"<b>{esc(pokemon.display_name)}</b> needs to know "
                f"<b>Dragon Ascent</b> to Mega Evolve!"
            )
            return

        # No stone to consume, just swap species
        old_name = esc(pokemon.display_name)
        pokemon.species_id = chosen.mega_dex
        await session.commit()
        await session.refresh(pokemon)

        await message.answer(
            f"🌀 <b>{old_name}</b> Mega Evolved into "
            f"<b>{esc(pokemon.display_name)}</b>!\n\n"
            f"<i>Use /demega to revert.</i>"
        )
        logger.info(
            "Mega evolution (Rayquaza)",
            user_id=user.telegram_id,
            pokemon_id=str(pokemon.id),
            mega_dex=chosen.mega_dex,
        )
        return

    # Normal mega — check for stone in inventory
    stone_item_id = _find_stone_item_id(chosen.mega_stone)
    if stone_item_id is None:
        await message.answer("Internal error: unknown mega stone.")
        return

    inv_result = await session.execute(
        select(InventoryItem)
        .where(InventoryItem.user_id == user.telegram_id)
        .where(InventoryItem.item_id == stone_item_id)
    )
    inventory_item = inv_result.scalar_one_or_none()

    if not inventory_item or inventory_item.quantity < 1:
        await message.answer(
            f"You need a <b>{chosen.mega_stone_display}</b> to Mega Evolve "
            f"<b>{esc(pokemon.display_name)}</b>!\n\n"
            f"Buy one from the shop: <code>/buy {stone_item_id}</code>"
        )
        return

    # Consume stone, swap species, store stone in held_item for /demega
    inventory_item.quantity -= 1
    old_name = esc(pokemon.display_name)
    pokemon.species_id = chosen.mega_dex
    pokemon.held_item = chosen.mega_stone  # remember which stone was used
    await session.commit()
    await session.refresh(pokemon)

    await message.answer(
        f"🌀 <b>{old_name}</b> Mega Evolved into "
        f"<b>{esc(pokemon.display_name)}</b>!\n\n"
        f"The <b>{chosen.mega_stone_display}</b> was consumed.\n"
        f"<i>Use /demega to revert and get the stone back.</i>"
    )
    logger.info(
        "Mega evolution",
        user_id=user.telegram_id,
        pokemon_id=str(pokemon.id),
        mega_dex=chosen.mega_dex,
        stone=chosen.mega_stone,
    )


# ──────────────────────────────────────────────
# /demega  — revert a Mega Pokémon to base form
# ──────────────────────────────────────────────

@router.message(Command("demega"))
async def cmd_demega(message: Message, session: AsyncSession, user: User) -> None:
    """Revert a Mega Pokémon to its base form, returning the Mega Stone.

    Usage:
        /demega           — revert selected Pokémon
        /demega <id>      — revert Pokémon #id
    """
    if not message.from_user:
        return

    args = (message.text or "").split()[1:]
    poke_idx = args[0] if args else None

    pokemon = await _resolve_pokemon(session, user, poke_idx)
    if not pokemon:
        await message.answer(
            "Couldn't find that Pokémon. "
            "Usage: <code>/demega [id]</code>"
        )
        return

    # Check if actually mega
    base_info = get_base_species_for_mega_dex(pokemon.species_id)
    if base_info is None:
        await message.answer(
            f"<b>{esc(pokemon.display_name)}</b> is not Mega Evolved!"
        )
        return

    base_species_id, stone_name = base_info

    # Revert species
    old_name = esc(pokemon.display_name)
    pokemon.species_id = base_species_id

    # Return stone to inventory (if one was used)
    returned_stone = None
    stone_to_return = pokemon.held_item or stone_name
    if stone_to_return:
        stone_item_id = _find_stone_item_id(stone_to_return)
        if stone_item_id:
            inv_result = await session.execute(
                select(InventoryItem)
                .where(InventoryItem.user_id == user.telegram_id)
                .where(InventoryItem.item_id == stone_item_id)
            )
            inv_item = inv_result.scalar_one_or_none()
            if inv_item:
                inv_item.quantity += 1
            else:
                session.add(InventoryItem(
                    user_id=user.telegram_id,
                    item_id=stone_item_id,
                    quantity=1,
                ))
            returned_stone = MEGA_STONE_BY_NAME.get(stone_to_return, {}).get("name", stone_to_return)
        pokemon.held_item = None

    await session.commit()
    await session.refresh(pokemon)

    stone_msg = f"\n<b>{returned_stone}</b> returned to your inventory." if returned_stone else ""
    await message.answer(
        f"🔄 <b>{old_name}</b> reverted to "
        f"<b>{esc(pokemon.display_name)}</b>!{stone_msg}"
    )
    logger.info(
        "De-mega evolution",
        user_id=user.telegram_id,
        pokemon_id=str(pokemon.id),
        base_species=base_species_id,
        stone_returned=returned_stone,
    )
