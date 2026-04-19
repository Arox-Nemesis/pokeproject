"""Breeding handlers — daycare, breed, eggs, hatch commands."""

from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.core.breeding import (
    add_steps_to_eggs,
    add_to_daycare,
    check_compatibility,
    create_egg,
    get_daycare_slots,
    get_user_eggs,
    hatch_egg,
    remove_from_daycare,
)
from telemon.core.constants import BREED_COOLDOWN_SECONDS, iv_percentage
from telemon.database.models import Pokemon, User
from telemon.logging import get_logger

router = Router(name="breeding")
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Cooldown Tracking
# ---------------------------------------------------------------------------
_breed_cooldowns: dict[int, datetime] = {}
_BREED_COOLDOWN_MAX_SIZE = 1000



# ---------------------------------------------------------------------------
# Helper: resolve a Pokemon by index number
# ---------------------------------------------------------------------------

async def _get_pokemon_by_index(
    session: AsyncSession, user_id: int, index: int
) -> Pokemon | None:
    """Get a user's Pokemon by 1-based index (ordered by caught_at)."""
    result = await session.execute(
        select(Pokemon)
        .where(Pokemon.owner_id == user_id)
        .order_by(Pokemon.caught_at)
        .offset(index - 1)
        .limit(1)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# /daycare — view, add, remove
# ---------------------------------------------------------------------------

@router.message(Command("daycare"))
async def cmd_daycare(message: Message, session: AsyncSession) -> None:
    """Handle /daycare command."""
    if not message.from_user:
        return

    user_id = message.from_user.id
    args = (message.text or "").split()

    # /daycare add [#]
    if len(args) >= 3 and args[1].lower() == "add":
        try:
            index = int(args[2])
        except ValueError:
            await message.answer("Usage: /daycare add [pokemon#]")
            return
        await _daycare_add(message, session, user_id, index)
        return

    # /daycare remove [slot]
    if len(args) >= 3 and args[1].lower() == "remove":
        try:
            slot_num = int(args[2])
        except ValueError:
            await message.answer("Usage: /daycare remove [1 or 2]")
            return
        await _daycare_remove(message, session, user_id, slot_num)
        return

    # /daycare add (no index — use selected)
    if len(args) >= 2 and args[1].lower() == "add":
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user = result.scalar_one_or_none()
        if not user or not user.selected_pokemon_id:
            await message.answer("Select a Pokemon first with /select [#], or use /daycare add [#].")
            return

        pokemon = await session.get(Pokemon, user.selected_pokemon_id)
        if not pokemon:
            await message.answer("Selected Pokemon not found.")
            return

        ok, msg = await add_to_daycare(session, user_id, pokemon)
        if ok:
            await session.commit()
        await message.answer(msg)
        return

    # /daycare remove (no slot number)
    if len(args) >= 2 and args[1].lower() == "remove":
        await message.answer("Usage: /daycare remove [1 or 2]")
        return

    # Default: show daycare status
    await _daycare_status(message, session, user_id)


async def _daycare_add(
    message: Message, session: AsyncSession, user_id: int, index: int
) -> None:
    """Add a Pokemon to daycare by index."""
    pokemon = await _get_pokemon_by_index(session, user_id, index)
    if not pokemon:
        await message.answer(f"No Pokemon at index #{index}.")
        return

    ok, msg = await add_to_daycare(session, user_id, pokemon)
    if ok:
        await session.commit()
    await message.answer(msg)


async def _daycare_remove(
    message: Message, session: AsyncSession, user_id: int, slot_num: int
) -> None:
    """Remove a Pokemon from daycare."""
    if slot_num not in (1, 2):
        await message.answer("Slot must be 1 or 2.")
        return

    ok, msg = await remove_from_daycare(session, user_id, slot_num)
    if ok:
        await session.commit()
    await message.answer(msg)


async def _daycare_status(
    message: Message, session: AsyncSession, user_id: int
) -> None:
    """Show daycare status."""
    slots = await get_daycare_slots(session, user_id)
    eggs = await get_user_eggs(session, user_id)

    lines = ["<b>Daycare</b>\n"]

    if not slots:
        lines.append("No Pokemon in daycare.")
        lines.append("\nUse <code>/daycare add [#]</code> to place Pokemon.")
    else:
        for slot in slots:
            pokemon = slot.pokemon
            if pokemon and pokemon.species:
                shiny = " ✨" if pokemon.is_shiny else ""
                iv_pct = pokemon.iv_percentage
                lines.append(
                    f"Slot {slot.slot}: <b>{pokemon.display_name}</b>{shiny} "
                    f"(Lv.{pokemon.level} | {iv_pct:.1f}% IV)"
                )
            else:
                lines.append(f"Slot {slot.slot}: Pokemon #{slot.pokemon_id}")

        # Compatibility check
        if len(slots) == 2:
            p1, p2 = slots[0].pokemon, slots[1].pokemon
            if p1 and p2 and p1.species and p2.species:
                can_breed, compat_msg = check_compatibility(
                    p1.species, p2.species, p1.gender, p2.gender
                )
                emoji = "💕" if can_breed else "💔"
                lines.append(f"\n{emoji} {compat_msg}")
                if can_breed:
                    lines.append("Use <code>/breed</code> to produce an egg!")
        elif len(slots) == 1:
            lines.append("\nAdd a second Pokemon to breed!")

    # Show eggs
    if eggs:
        lines.append(f"\n<b>Eggs ({len(eggs)}/6)</b>")
        for i, egg in enumerate(eggs, 1):
            species_name = egg.species.name if egg.species else f"#{egg.species_id}"
            progress = max(0, egg.steps_total - egg.steps_remaining)
            pct = (progress / egg.steps_total * 100) if egg.steps_total > 0 else 100
            shiny = " ✨" if egg.is_shiny else ""

            if egg.steps_remaining <= 0:
                lines.append(f"  {i}. 🥚 <b>{species_name}</b>{shiny} — Ready to hatch!")
            else:
                bar = _progress_bar(pct)
                lines.append(
                    f"  {i}. 🥚 {species_name}{shiny} — {bar} {pct:.0f}% "
                    f"({egg.steps_remaining} steps left)"
                )
    else:
        lines.append(f"\n<b>Eggs (0/6)</b>")
        lines.append("No eggs. Breed compatible Pokemon to get one!")

    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# /breed — attempt to produce an egg
# ---------------------------------------------------------------------------

@router.message(Command("breed"))
async def cmd_breed(message: Message, session: AsyncSession) -> None:
    """Attempt to breed the two Pokemon in daycare."""
    if not message.from_user:
        return

    user_id = message.from_user.id
    now = datetime.utcnow()

    # Cooldown check
    if user_id in _breed_cooldowns:
        elapsed = (now - _breed_cooldowns[user_id]).total_seconds()
        if elapsed < BREED_COOLDOWN_SECONDS:
            remaining = int(BREED_COOLDOWN_SECONDS - elapsed)
            mins = remaining // 60
            secs = remaining % 60
            await message.answer(f"⏳ Your Pokemon need to rest! You can breed again in {mins}m {secs}s.")
            return

    # Prune expired cooldowns to prevent memory leak
    if len(_breed_cooldowns) > _BREED_COOLDOWN_MAX_SIZE:
        cutoff = now - timedelta(seconds=BREED_COOLDOWN_SECONDS)
        expired = [uid for uid, ts in _breed_cooldowns.items() if ts < cutoff]
        for uid in expired:
            del _breed_cooldowns[uid]

    slots = await get_daycare_slots(session, user_id)

    if len(slots) < 2:
        await message.answer(
            "You need 2 Pokemon in daycare to breed.\n"
            "Use <code>/daycare add [#]</code> to place Pokemon."
        )
        return

    p1, p2 = slots[0].pokemon, slots[1].pokemon
    if not p1 or not p2 or not p1.species or not p2.species:
        await message.answer("Could not load daycare Pokemon. Try again.")
        return

    # Check compatibility
    can_breed, compat_msg = check_compatibility(
        p1.species, p2.species, p1.gender, p2.gender
    )
    if not can_breed:
        await message.answer(f"💔 Cannot breed: {compat_msg}")
        return

    # Create egg
    egg = await create_egg(session, user_id, p1, p2)
    if egg is None:
        await message.answer("You have too many eggs (max 6)! Hatch some first with /hatch.")
        return

    await session.commit()

    # Set cooldown on successful breed
    _breed_cooldowns[user_id] = datetime.utcnow()

    species_name = egg.species.name if egg.species else f"#{egg.species_id}"
    shiny_text = " ✨" if egg.is_shiny else ""
    iv_total = (
        egg.iv_hp + egg.iv_attack + egg.iv_defense
        + egg.iv_sp_attack + egg.iv_sp_defense + egg.iv_speed
    )
    iv_pct = iv_percentage(iv_total)

    await message.answer(
        f"🥚 <b>An egg appeared!</b>{shiny_text}\n"
        f"Species: <b>{species_name}</b> | IV: {iv_pct}% | Steps: {egg.steps_total}\n"
        f"<i>Send messages in groups to add steps! Use /eggs to check, /hatch when ready.</i>"
    )


# ---------------------------------------------------------------------------
# /eggs — list eggs
# ---------------------------------------------------------------------------

@router.message(Command("eggs"))
async def cmd_eggs(message: Message, session: AsyncSession) -> None:
    """Show egg list."""
    if not message.from_user:
        return

    user_id = message.from_user.id
    eggs = await get_user_eggs(session, user_id)

    if not eggs:
        await message.answer(
            "🥚 No eggs.\nBreed Pokemon in the daycare with <code>/breed</code>!"
        )
        return

    lines = [f"<b>Your Eggs ({len(eggs)}/6)</b>\n"]
    for i, egg in enumerate(eggs, 1):
        species_name = egg.species.name if egg.species else f"#{egg.species_id}"
        progress = max(0, egg.steps_total - egg.steps_remaining)
        pct = (progress / egg.steps_total * 100) if egg.steps_total > 0 else 100
        shiny = " ✨" if egg.is_shiny else ""

        iv_total = (
            egg.iv_hp + egg.iv_attack + egg.iv_defense
            + egg.iv_sp_attack + egg.iv_sp_defense + egg.iv_speed
        )
        iv_pct = iv_percentage(iv_total)

        if egg.steps_remaining <= 0:
            lines.append(
                f"{i}. 🥚 <b>{species_name}</b>{shiny} — "
                f"<b>Ready to hatch!</b> ({iv_pct}% IV)"
            )
        else:
            bar = _progress_bar(pct)
            lines.append(
                f"{i}. 🥚 {species_name}{shiny} — {bar} {pct:.0f}% "
                f"({egg.steps_remaining} steps) | {iv_pct}% IV"
            )

    ready_count = sum(1 for e in eggs if e.steps_remaining <= 0)
    if ready_count:
        lines.append(f"\n<b>{ready_count} egg(s) ready!</b> Use <code>/hatch</code>")

    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# /hatch — hatch ready eggs
# ---------------------------------------------------------------------------

@router.message(Command("hatch"))
async def cmd_hatch(message: Message, session: AsyncSession) -> None:
    """Hatch all ready eggs."""
    if not message.from_user:
        return

    user_id = message.from_user.id
    eggs = await get_user_eggs(session, user_id)

    ready = [e for e in eggs if e.steps_remaining <= 0]
    if not ready:
        if eggs:
            await message.answer(
                "No eggs are ready yet. Keep sending messages to add steps!\n"
                "Use <code>/eggs</code> to check progress."
            )
        else:
            await message.answer("You have no eggs.")
        return

    hatched_lines: list[str] = []
    for egg in ready:
        try:
            pokemon = await hatch_egg(session, egg)
            shiny = " ✨" if pokemon.is_shiny else ""
            species_name = pokemon.species.name if pokemon.species else f"#{pokemon.species_id}"
            iv_pct = pokemon.iv_percentage
            hatched_lines.append(
                f"🐣 <b>{species_name}</b>{shiny} hatched! "
                f"(Lv.1 | {iv_pct:.1f}% IV | {pokemon.nature.title()} nature)"
            )
        except Exception as e:
            logger.error("Failed to hatch egg", egg_id=str(egg.id), error=str(e))
            hatched_lines.append(f"Failed to hatch an egg: {e}")

    await session.commit()

    header = "🎉 <b>Eggs Hatched!</b>\n\n" if len(hatched_lines) > 1 else ""
    await message.answer(header + "\n".join(hatched_lines))


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _progress_bar(pct: float, length: int = 10) -> str:
    """Build a text progress bar."""
    filled = int(pct / 100 * length)
    empty = length - filled
    return "▓" * filled + "░" * empty
