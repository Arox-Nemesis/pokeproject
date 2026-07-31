"""Move management handlers — view, learn, forget, and list moves."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.core.moves import (
    forget_move,
    get_learnable_moves,
    get_pokemon_known_moves,
    learn_move,
)
from telemon.database.models import Pokemon, User
from telemon.logging import get_logger
from telemon.core.text import esc

router = Router(name="moves")
logger = get_logger(__name__)

# Type emoji mapping for display
TYPE_EMOJI = {
    "normal": "⬜", "fire": "🔥", "water": "💧", "electric": "⚡",
    "grass": "🌿", "ice": "❄️", "fighting": "🥊", "poison": "☠️",
    "ground": "🌍", "flying": "🕊️", "psychic": "🔮", "bug": "🐛",
    "rock": "🪨", "ghost": "👻", "dragon": "🐉", "dark": "🌑",
    "steel": "⚙️", "fairy": "✨",
}


async def _resolve_pokemon(
    session: AsyncSession, user: User, arg: str | None
) -> Pokemon | None:
    """Resolve a Pokemon from argument or selected Pokemon."""
    if arg and arg.isdigit():
        idx = int(arg)
        result = await session.execute(
            select(Pokemon)
            .where(Pokemon.owner_id == user.telegram_id)
            .order_by(Pokemon.caught_at.asc())
            .offset(idx - 1)
            .limit(1)
        )
        return result.scalar_one_or_none()

    if user.selected_pokemon_id:
        result = await session.execute(
            select(Pokemon)
            .where(Pokemon.id == user.selected_pokemon_id)
            .where(Pokemon.owner_id == user.telegram_id)
        )
        return result.scalar_one_or_none()

    return None


@router.message(Command("moves"))
async def cmd_moves(message: Message, session: AsyncSession, user: User) -> None:
    """View a Pokemon's current moves."""
    text = message.text or ""
    args = text.split()
    arg = args[1] if len(args) >= 2 else None

    poke = await _resolve_pokemon(session, user, arg)
    if not poke:
        await message.answer(
            "Pokemon not found!\n"
            "Usage: /moves [number] or select one with /select"
        )
        return

    known = await get_pokemon_known_moves(session, poke)

    if not known:
        await message.answer(
            f"<b>{esc(poke.display_name)}</b> doesn't know any moves yet.\n"
            "Use /learn [move] to teach it a move, or catch a new Pokemon (moves are auto-assigned)."
        )
        return

    lines = [
        f"<b>{esc(poke.display_name)}</b> — Moves\n",
    ]

    for i, move in enumerate(known, 1):
        emoji = TYPE_EMOJI.get(move.type, "")
        power = f"Pow: {move.power}" if move.power else "Status"
        acc = f"Acc: {move.accuracy}%" if move.accuracy else "—"
        pp = f"PP: {move.pp}" if move.pp else ""
        cat = move.category.capitalize() if move.category else ""

        lines.append(
            f"{i}. {emoji} <b>{move.name}</b> ({move.type.capitalize()})\n"
            f"   {cat} | {power} | {acc} | {pp}"
        )

    lines.append(f"\n<i>Use /learn [move] to learn a new move</i>")
    lines.append(f"<i>Use /forget [move] to forget a move</i>")
    lines.append(f"<i>Use /learnable to see all available moves</i>")

    await message.answer("\n".join(lines))


@router.message(Command("learn"))
async def cmd_learn(message: Message, session: AsyncSession, user: User) -> None:
    """Learn a move for the selected Pokemon."""
    text = message.text or ""
    args = text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer(
            "<b>Usage:</b>\n"
            "/learn [move name] — Teach your selected Pokemon a move\n\n"
            "<b>Example:</b> /learn flamethrower\n\n"
            "<i>Use /learnable to see what moves your Pokemon can learn.</i>"
        )
        return

    move_name = args[1].strip()

    # Resolve selected Pokemon
    poke = await _resolve_pokemon(session, user, None)
    if not poke:
        await message.answer("No Pokemon selected! Use /select [number] first.")
        return

    success, msg = await learn_move(session, poke, move_name)
    if success:
        await session.commit()
    await message.answer(msg)


@router.message(Command("forget"))
async def cmd_forget(message: Message, session: AsyncSession, user: User) -> None:
    """Forget a move from the selected Pokemon."""
    text = message.text or ""
    args = text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer(
            "<b>Usage:</b>\n"
            "/forget [move name] — Forget a move from your selected Pokemon\n\n"
            "<b>Example:</b> /forget tackle\n\n"
            "<i>Use /moves to see your Pokemon's current moves.</i>"
        )
        return

    move_name = args[1].strip()

    poke = await _resolve_pokemon(session, user, None)
    if not poke:
        await message.answer("No Pokemon selected! Use /select [number] first.")
        return

    success, msg = await forget_move(session, poke, move_name)
    if success:
        await session.commit()
    await message.answer(msg)


@router.message(Command("learnable", "movelist"))
async def cmd_learnable(message: Message, session: AsyncSession, user: User) -> None:
    """Show all moves a Pokemon can learn at its current level."""
    text = message.text or ""
    args = text.split()
    arg = args[1] if len(args) >= 2 else None

    poke = await _resolve_pokemon(session, user, arg)
    if not poke:
        await message.answer(
            "Pokemon not found!\n"
            "Usage: /learnable [number] or select one with /select"
        )
        return

    learnable = await get_learnable_moves(session, poke.species_id, poke.level)

    if not learnable:
        await message.answer(
            f"<b>{esc(poke.display_name)}</b> has no level-up moves available.\n"
            "<i>This species may not have learnset data yet.</i>"
        )
        return

    # Get currently known moves for marking
    known_lower = set(m.lower() for m in (poke.moves or []))

    lines = [
        f"<b>{esc(poke.display_name)}</b> — Learnable Moves (Lv.{poke.level})\n",
    ]

    for entry in learnable:
        move = entry["move"]
        lvl = entry["level"]
        emoji = TYPE_EMOJI.get(move.type, "")
        power = f"Pow:{move.power}" if move.power else "Status"
        known_mark = " [Known]" if move.name_lower in known_lower else ""

        lines.append(
            f"Lv.{lvl:>3} {emoji} <b>{move.name}</b>{known_mark} "
            f"({move.type.capitalize()}) {power}"
        )

    # Cap output to avoid Telegram message limit
    if len(lines) > 50:
        total = len(lines) - 1  # subtract header
        lines = lines[:51]
        lines.append(f"\n<i>...and {total - 50} more moves</i>")

    lines.append(f"\n<i>Use /learn [move] to teach a move (max 4)</i>")

    await message.answer("\n".join(lines))
