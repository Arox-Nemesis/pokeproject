"""Pokedex-related handlers for tracking Pokemon collection progress."""

import math
import re
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.core.constants import MAX_GENERATION
from telemon.core.emoji import poke_emoji, type_emoji
from telemon.core.forms import get_mega_forms
from telemon.database.models import PokedexEntry, Pokemon, PokemonSpecies, User
from telemon.logging import get_logger

router = Router(name="pokedex")
logger = get_logger(__name__)

# Known form prefixes to strip when searching
FORM_PREFIXES = ("mega", "alolan", "galarian", "hisuian", "paldean", "galar", "alola", "hisui", "paldea")

# Constants
ENTRIES_PER_PAGE = 10

# Generation names for display
GEN_NAMES = {
    1: "Kanto",
    2: "Johto",
    3: "Hoenn",
    4: "Sinnoh",
    5: "Unova",
    6: "Kalos",
    7: "Alola",
    8: "Galar",
    9: "Paldea",
}


def parse_pokedex_args(text: str) -> dict:
    """Parse arguments from pokedex command.

    Supports:
      /pokedex gen:3
      /pokedex list gen:3
      /pokedex caught gen:1
      /pokedex --gen 3
    """
    args = {
        "subcommand": None,
        "gen": None,
        "page": 1,
        "query": None,
    }

    if not text:
        return args

    parts = text.split()
    i = 0
    positional_args = []

    while i < len(parts):
        part = parts[i].lower()

        # Key:value style
        if ":" in part:
            key, _, value = part.partition(":")
            if key in ("gen", "g", "generation") and value.isdigit():
                gen = int(value)
                if 1 <= gen <= MAX_GENERATION:
                    args["gen"] = gen
            elif key in ("page", "p") and value.isdigit():
                args["page"] = max(1, int(value))
        # --gen N style
        elif part in ("--gen", "--generation"):
            if i + 1 < len(parts) and parts[i + 1].isdigit():
                gen = int(parts[i + 1])
                if 1 <= gen <= MAX_GENERATION:
                    args["gen"] = gen
                i += 1
        # Plain page number
        elif part.isdigit():
            num = int(part)
            if 1 <= num <= 9 and args["gen"] is None and args["subcommand"] is None:
                # Ambiguous — could be gen or page. Treat as page.
                args["page"] = num
            else:
                args["page"] = max(1, num)
        else:
            positional_args.append(part)

        i += 1

    # First positional arg is the subcommand
    if positional_args:
        args["subcommand"] = positional_args[0]
        if len(positional_args) > 1:
            args["query"] = " ".join(positional_args[1:])

    return args


async def get_total_pokemon_count(session: AsyncSession, gen: int | None = None) -> int:
    """Get total Pokemon count, optionally filtered by generation."""
    query = select(func.count(PokemonSpecies.national_dex))
    if gen is not None:
        query = query.where(PokemonSpecies.generation == gen)
    result = await session.execute(query)
    return result.scalar() or 0


async def get_gen_counts(session: AsyncSession) -> dict[int, int]:
    """Get Pokemon count per generation."""
    result = await session.execute(
        select(PokemonSpecies.generation, func.count(PokemonSpecies.national_dex))
        .group_by(PokemonSpecies.generation)
        .order_by(PokemonSpecies.generation)
    )
    return dict(result.all())


async def get_pokedex_stats(
    session: AsyncSession, user_id: int, gen: int | None = None
) -> dict:
    """Get pokedex completion statistics for a user, optionally by generation."""
    # Get species IDs in this gen (if filtered)
    gen_filter = None
    if gen is not None:
        gen_species = await session.execute(
            select(PokemonSpecies.national_dex)
            .where(PokemonSpecies.generation == gen)
        )
        gen_filter = [s for s in gen_species.scalars().all()]

    def apply_gen_filter(query):
        if gen_filter is not None:
            return query.where(PokedexEntry.species_id.in_(gen_filter))
        return query

    # Count seen
    seen_q = select(func.count(PokedexEntry.species_id)).where(
        PokedexEntry.user_id == user_id,
        PokedexEntry.seen == True,
    )
    seen_result = await session.execute(apply_gen_filter(seen_q))
    seen_count = seen_result.scalar() or 0

    # Count caught
    caught_q = select(func.count(PokedexEntry.species_id)).where(
        PokedexEntry.user_id == user_id,
        PokedexEntry.caught == True,
    )
    caught_result = await session.execute(apply_gen_filter(caught_q))
    caught_count = caught_result.scalar() or 0

    # Count shiny caught
    shiny_q = select(func.count(PokedexEntry.species_id)).where(
        PokedexEntry.user_id == user_id,
        PokedexEntry.caught_shiny == True,
    )
    shiny_result = await session.execute(apply_gen_filter(shiny_q))
    shiny_count = shiny_result.scalar() or 0

    # Total catches (sum of times_caught)
    total_q = select(func.sum(PokedexEntry.times_caught)).where(
        PokedexEntry.user_id == user_id,
    )
    total_catches_result = await session.execute(apply_gen_filter(total_q))
    total_catches = total_catches_result.scalar() or 0

    # Total pokemon in scope
    total_pokemon = await get_total_pokemon_count(session, gen)

    return {
        "seen": seen_count,
        "caught": caught_count,
        "shiny": shiny_count,
        "total_catches": total_catches,
        "total_pokemon": total_pokemon,
        "seen_percent": round((seen_count / total_pokemon) * 100, 1) if total_pokemon else 0,
        "caught_percent": round((caught_count / total_pokemon) * 100, 1) if total_pokemon else 0,
    }


async def get_caught_per_gen(session: AsyncSession, user_id: int) -> dict[int, int]:
    """Get number of caught unique species per generation."""
    result = await session.execute(
        select(PokemonSpecies.generation, func.count(PokedexEntry.species_id))
        .join(PokemonSpecies, PokemonSpecies.national_dex == PokedexEntry.species_id)
        .where(PokedexEntry.user_id == user_id, PokedexEntry.caught == True)
        .group_by(PokemonSpecies.generation)
        .order_by(PokemonSpecies.generation)
    )
    return dict(result.all())


async def get_pokedex_entries(
    session: AsyncSession,
    user_id: int,
    page: int = 1,
    filter_type: str = "all",  # all, caught, missing, shiny, seen
    gen: int | None = None,
) -> tuple[list[dict], int]:
    """Get pokedex entries with filters."""
    # Get all species (filtered by gen if specified)
    species_q = select(PokemonSpecies).order_by(PokemonSpecies.national_dex)
    if gen is not None:
        species_q = species_q.where(PokemonSpecies.generation == gen)

    species_result = await session.execute(species_q)
    all_species = list(species_result.scalars().all())

    # Get user's pokedex entries
    entries_result = await session.execute(
        select(PokedexEntry).where(PokedexEntry.user_id == user_id)
    )
    user_entries = {e.species_id: e for e in entries_result.scalars().all()}

    # Build filtered list
    filtered_entries = []
    for species in all_species:
        entry = user_entries.get(species.national_dex)
        seen = entry.seen if entry else False
        caught = entry.caught if entry else False
        caught_shiny = entry.caught_shiny if entry else False
        times_caught = entry.times_caught if entry else 0

        entry_data = {
            "dex_num": species.national_dex,
            "name": species.name,
            "type1": species.type1,
            "type2": species.type2,
            "generation": species.generation,
            "seen": seen,
            "caught": caught,
            "caught_shiny": caught_shiny,
            "times_caught": times_caught,
        }

        # Apply filter
        if filter_type == "all":
            filtered_entries.append(entry_data)
        elif filter_type == "caught" and caught:
            filtered_entries.append(entry_data)
        elif filter_type == "missing" and not caught:
            filtered_entries.append(entry_data)
        elif filter_type == "shiny" and caught_shiny:
            filtered_entries.append(entry_data)
        elif filter_type == "seen" and seen and not caught:
            filtered_entries.append(entry_data)

    # Paginate
    total_count = len(filtered_entries)
    start_idx = (page - 1) * ENTRIES_PER_PAGE
    end_idx = start_idx + ENTRIES_PER_PAGE
    page_entries = filtered_entries[start_idx:end_idx]

    return page_entries, total_count


def resolve_form_query(query: str) -> tuple[str, str | None]:
    """Detect and strip form prefixes from a query.

    Returns (base_name, form_type) where form_type is e.g. 'mega',
    'alolan', etc., or None if no prefix was found.
    """
    q = query.strip().lower()
    for prefix in FORM_PREFIXES:
        if q.startswith(prefix + " "):
            base = q[len(prefix):].strip()
            # Normalise prefix to canonical form
            canonical = prefix
            if canonical in ("alola",):
                canonical = "alolan"
            elif canonical in ("galar",):
                canonical = "galarian"
            elif canonical in ("hisui",):
                canonical = "hisuian"
            elif canonical in ("paldea",):
                canonical = "paldean"
            return base, canonical
    return query.strip(), None


async def get_species_by_name_or_number(
    session: AsyncSession, query: str
) -> PokemonSpecies | None:
    """Find a Pokemon species by name or dex number.

    Priority: exact match → starts-with → contains.
    Uses the full multi-word query to avoid false matches.
    """
    query = query.strip()

    # Try as number first
    if query.isdigit():
        result = await session.execute(
            select(PokemonSpecies)
            .where(PokemonSpecies.national_dex == int(query))
        )
        return result.scalar_one_or_none()

    # 1. Exact match (case-insensitive)
    result = await session.execute(
        select(PokemonSpecies)
        .where(PokemonSpecies.name.ilike(query))
    )
    species = result.scalar_one_or_none()
    if species:
        return species

    # 2. Starts-with match
    result = await session.execute(
        select(PokemonSpecies)
        .where(PokemonSpecies.name.ilike(f"{query}%"))
        .order_by(PokemonSpecies.national_dex)
        .limit(1)
    )
    species = result.scalar_one_or_none()
    if species:
        return species

    # 3. Contains match (last resort)
    result = await session.execute(
        select(PokemonSpecies)
        .where(PokemonSpecies.name.ilike(f"%{query}%"))
        .order_by(PokemonSpecies.national_dex)
        .limit(1)
    )
    return result.scalar_one_or_none()


def build_pokedex_keyboard(
    page: int, total_pages: int, filter_type: str = "all", gen: int | None = None
) -> InlineKeyboardBuilder:
    """Build pagination keyboard for pokedex."""
    builder = InlineKeyboardBuilder()
    gen_str = str(gen) if gen else "0"

    # Pagination row
    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(("◀️", f"dex:page:{page - 1}:{filter_type}:{gen_str}"))
        nav_buttons.append((f"{page}/{total_pages}", "dex:noop"))
        if page < total_pages:
            nav_buttons.append(("▶️", f"dex:page:{page + 1}:{filter_type}:{gen_str}"))

        for text, callback_data in nav_buttons:
            builder.button(text=text, callback_data=callback_data)

    # Filter row
    filter_buttons = [
        ("All", "all"),
        ("Caught", "caught"),
        ("Missing", "missing"),
        ("Shiny", "shiny"),
    ]

    for text, ftype in filter_buttons:
        display = f"[{text}]" if filter_type == ftype else text
        builder.button(text=display, callback_data=f"dex:filter:{ftype}:1:{gen_str}")

    builder.adjust(3, 4)  # 3 nav buttons, 4 filter buttons

    return builder


def format_dex_entry_line(entry: dict, show_details: bool = False) -> str:
    """Format a single pokedex entry for list display."""
    dex_num = entry["dex_num"]
    name = entry["name"]
    caught = entry["caught"]
    seen = entry["seen"]
    shiny = entry["caught_shiny"]

    # Status icon
    if caught:
        if shiny:
            status = "✨"
        else:
            status = "✅"
    elif seen:
        status = "👁️"
    else:
        status = "❓"

    # Type display
    types = entry["type1"].title()
    if entry["type2"]:
        types += f"/{entry['type2'].title()}"

    sprite = poke_emoji(dex_num) if (caught or seen) else ""

    if show_details and caught:
        return f"{status} #{dex_num:03d} {sprite}<b>{name}</b> [{types}] (x{entry['times_caught']})"
    elif caught or seen:
        return f"{status} #{dex_num:03d} {sprite}<b>{name}</b> [{types}]"
    else:
        return f"{status} #{dex_num:03d} ???"


def generate_progress_bar(percent: float, width: int = 10) -> str:
    """Generate a text-based progress bar."""
    filled = int(percent / 100 * width)
    empty = width - filled
    return "█" * filled + "░" * empty


@router.message(Command("pokedex", "dex"))
async def cmd_pokedex(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /pokedex command and subcommands."""
    text = message.text or ""
    # Strip command prefix and bot mention (e.g. /pokedex@TelemonXRobot)
    raw = re.sub(r"^/(?:pokedex|dex)(?:@\S+)?", "", text, count=1).strip()

    args = parse_pokedex_args(raw)
    sub = args["subcommand"]
    gen = args["gen"]

    if sub is None:
        await show_pokedex_overview(message, session, user, gen=gen)
        return

    if sub in ("list", "all"):
        await show_pokedex_list(message, session, user, filter_type="all", page=args["page"], gen=gen)
    elif sub in ("caught", "owned"):
        await show_pokedex_list(message, session, user, filter_type="caught", page=args["page"], gen=gen)
    elif sub in ("missing", "uncaught", "needed"):
        await show_pokedex_list(message, session, user, filter_type="missing", page=args["page"], gen=gen)
    elif sub in ("shiny", "shinies"):
        await show_pokedex_list(message, session, user, filter_type="shiny", page=args["page"], gen=gen)
    elif sub in ("seen",):
        await show_pokedex_list(message, session, user, filter_type="seen", page=args["page"], gen=gen)
    elif sub in ("search", "find"):
        # Rejoin subcommand + query for multi-word searches like "search Mega Charizard X"
        full_query = args["query"]
        if full_query:
            await pokedex_search(message, session, user, full_query)
        else:
            await message.answer("Usage: /pokedex search [name or number]")
    elif sub == "help":
        await pokedex_help(message)
    else:
        # Rejoin subcommand + any extra query for multi-word names
        # e.g. "/pokedex Mega Charizard X" → sub='mega', query='charizard x'
        full_query = sub
        if args["query"]:
            full_query = f"{sub} {args['query']}"
        await pokedex_search(message, session, user, full_query)


async def show_pokedex_overview(
    message: Message, session: AsyncSession, user: User, gen: int | None = None
) -> None:
    """Show pokedex completion overview."""
    stats = await get_pokedex_stats(session, user.telegram_id, gen=gen)

    caught_bar = generate_progress_bar(stats["caught_percent"])
    seen_bar = generate_progress_bar(stats["seen_percent"])

    # Title
    if gen:
        gen_name = GEN_NAMES.get(gen, f"Gen {gen}")
        title = f"📕 <b>{user.display_name}'s Pokédex — Gen {gen} ({gen_name})</b>"
    else:
        title = f"📕 <b>{user.display_name}'s Pokédex</b>"

    lines = [
        title,
        "",
        f"<b>Caught:</b> {stats['caught']}/{stats['total_pokemon']} ({stats['caught_percent']}%)",
        f"[{caught_bar}]",
        "",
        f"<b>Seen:</b> {stats['seen']}/{stats['total_pokemon']} ({stats['seen_percent']}%)",
        f"[{seen_bar}]",
        "",
        f"✨ <b>Shinies:</b> {stats['shiny']}",
        f"🎯 <b>Total Catches:</b> {stats['total_catches']}",
    ]

    # Per-generation breakdown (only in full overview)
    if gen is None:
        gen_counts = await get_gen_counts(session)
        caught_per_gen = await get_caught_per_gen(session, user.telegram_id)

        lines.append("")
        lines.append("<b>By Generation:</b>")
        for g in sorted(gen_counts.keys()):
            total = gen_counts[g]
            caught = caught_per_gen.get(g, 0)
            pct = round((caught / total) * 100) if total else 0
            region = GEN_NAMES.get(g, "???")
            bar = generate_progress_bar(pct, width=6)
            lines.append(f"  Gen {g} ({region}): {caught}/{total} [{bar}] {pct}%")

    # Recent catches
    recent_q = (
        select(PokedexEntry)
        .where(PokedexEntry.user_id == user.telegram_id, PokedexEntry.caught == True)
        .order_by(PokedexEntry.first_caught_at.desc())
        .limit(5)
    )
    if gen is not None:
        gen_species = await session.execute(
            select(PokemonSpecies.national_dex).where(PokemonSpecies.generation == gen)
        )
        gen_ids = [s for s in gen_species.scalars().all()]
        recent_q = recent_q.where(PokedexEntry.species_id.in_(gen_ids))

    recent_result = await session.execute(recent_q)
    recent_entries = list(recent_result.scalars().all())

    recent_lines = []
    for entry in recent_entries:
        shiny = "✨" if entry.caught_shiny else ""
        sprite = poke_emoji(entry.species_id)
        recent_lines.append(f"  {sprite}#{entry.species_id:03d} {entry.species.name}{shiny}")

    recent_text = "\n".join(recent_lines) if recent_lines else "  <i>None yet!</i>"

    lines.append("")
    lines.append(f"<b>Recent Catches:</b>\n{recent_text}")
    lines.append("")
    lines.append("<b>Commands:</b>")
    lines.append("/pokedex list - Browse all entries")
    lines.append("/pokedex caught - View caught Pokemon")
    lines.append("/pokedex missing - View uncaught Pokemon")
    lines.append("/pokedex [name/#] - Look up Pokemon")
    if gen is None:
        lines.append("/pokedex gen:N - Filter by generation (1-9)")
    else:
        lines.append(f"/pokedex list gen:{gen} - Browse Gen {gen}")

    await message.answer("\n".join(lines))


async def show_pokedex_list(
    message: Message,
    session: AsyncSession,
    user: User,
    filter_type: str = "all",
    page: int = 1,
    gen: int | None = None,
) -> None:
    """Show paginated pokedex list."""
    entries, total_count = await get_pokedex_entries(
        session, user.telegram_id, page=page, filter_type=filter_type, gen=gen
    )

    if not entries:
        filter_names = {
            "all": "entries",
            "caught": "caught Pokemon",
            "missing": "missing Pokemon",
            "shiny": "shiny Pokemon",
            "seen": "seen (but uncaught) Pokemon",
        }
        gen_text = f" in Gen {gen}" if gen else ""
        await message.answer(
            f"📕 <b>Pokédex</b>\n\n"
            f"No {filter_names.get(filter_type, 'entries')}{gen_text} found!"
        )
        return

    total_pages = math.ceil(total_count / ENTRIES_PER_PAGE)

    text = format_pokedex_list_text(entries, total_count, filter_type, gen)
    keyboard = build_pokedex_keyboard(page, total_pages, filter_type, gen)

    await message.answer(text, reply_markup=keyboard.as_markup())


def format_pokedex_list_text(
    entries: list[dict], total_count: int, filter_type: str, gen: int | None
) -> str:
    """Format the pokedex list message text."""
    filter_titles = {
        "all": "All Pokemon",
        "caught": "Caught Pokemon",
        "missing": "Missing Pokemon",
        "shiny": "Shiny Pokemon",
        "seen": "Seen (Uncaught)",
    }

    gen_text = f" — Gen {gen}" if gen else ""

    lines = [
        f"📕 <b>Pokédex - {filter_titles.get(filter_type, 'All')}{gen_text}</b>",
        f"<i>Showing {len(entries)} of {total_count}</i>\n",
    ]

    for entry in entries:
        lines.append(format_dex_entry_line(entry, show_details=True))

    lines.append("\n<b>Legend:</b> ✅ Caught | ✨ Shiny | 👁️ Seen | ❓ Unknown")

    return "\n".join(lines)


async def _build_evolution_chain_text(
    session: AsyncSession, species: PokemonSpecies
) -> str:
    """Build a 'Bulbasaur → Ivysaur → Venusaur' style evolution chain line."""
    import json
    from pathlib import Path

    chain_id = species.evolution_chain_id
    if not chain_id:
        return "<b>Evolution:</b> Does not evolve"

    try:
        evo_path = Path(__file__).parent.parent.parent.parent.parent / "data" / "evolutions.json"
        with open(evo_path) as f:
            all_chains = json.load(f)

        chain_data = all_chains.get(str(chain_id))
        if not chain_data or not chain_data.get("chain"):
            return "<b>Evolution:</b> Does not evolve"

        # Collect all species IDs in this chain
        chain_entries = chain_data["chain"]
        species_ids: set[int] = set()
        for entry in chain_entries:
            species_ids.add(entry["species_id"])
            species_ids.add(entry["evolves_to"])

        # Fetch names from DB
        result = await session.execute(
            select(PokemonSpecies.national_dex, PokemonSpecies.name)
            .where(PokemonSpecies.national_dex.in_(species_ids))
        )
        id_to_name: dict[int, str] = {row[0]: row[1] for row in result.all()}

        # Build ordered chain: find the base (species not in any evolves_to)
        evolves_to_set = {e["evolves_to"] for e in chain_entries}
        from_set = {e["species_id"] for e in chain_entries}
        bases = from_set - evolves_to_set

        if not bases:
            # Fallback — everything evolves from something, pick lowest dex
            bases = {min(species_ids)}

        # Walk from each base
        chains: list[list[str]] = []
        for base_id in sorted(bases):
            path = [base_id]
            current = base_id
            while True:
                nexts = [e["evolves_to"] for e in chain_entries if e["species_id"] == current]
                if not nexts:
                    break
                current = nexts[0]
                path.append(current)
            chain_names = []
            for sid in path:
                name = id_to_name.get(sid, f"#{sid}")
                sp = poke_emoji(sid)
                if sid == species.national_dex:
                    chain_names.append(f"{sp}<b>{name}</b>")
                else:
                    chain_names.append(f"{sp}{name}")
            chains.append(chain_names)

        if not chains:
            return "<b>Evolution:</b> Does not evolve"

        # Usually just one chain, but branching evolutions (e.g. Eevee) produce many
        if len(chains) == 1:
            return f"<b>Evolution:</b> {' → '.join(chains[0])}"

        # For branching, show base → branch1 / branch2 ...
        # Common prefix
        base_name = chains[0][0]
        branch_ends = [c[-1] for c in chains if len(c) > 1]
        if len(branch_ends) <= 5:
            branches = " / ".join(branch_ends)
            return f"<b>Evolution:</b> {base_name} → {branches}"
        else:
            return f"<b>Evolution:</b> {base_name} → {len(branch_ends)} forms"

    except Exception as e:
        logger.debug("Evolution chain parse failed", error=str(e))
        return "<b>Evolution:</b> —"


async def pokedex_search(
    message: Message, session: AsyncSession, user: User, query: str
) -> None:
    """Search for and display a specific Pokemon entry.

    Handles form prefixes (Mega, Alolan, etc.) and shows a paginated
    inline-button view: Overview → Details → Forms.
    """
    base_query, form_type = resolve_form_query(query)
    species = await get_species_by_name_or_number(session, base_query)

    if not species:
        await message.answer(
            f"❌ Pokemon '{query}' not found.\n"
            "Try using the National Dex number or exact name."
        )
        return

    # If user asked for a form, jump directly to forms page
    start_page = "overview"
    if form_type == "mega":
        megas = get_mega_forms(species.national_dex)
        if megas:
            start_page = "forms"

    text, keyboard = await _build_entry_page(
        session, species, user.telegram_id, start_page
    )

    # Try to send with artwork image (overview only)
    if start_page == "overview":
        try:
            from aiogram.types import BufferedInputFile
            from telemon.core.imaging import generate_spawn_image

            image_data = await generate_spawn_image(
                dex_number=species.national_dex,
                primary_type=species.type1 or "normal",
                shiny=False,
            )
            if image_data:
                photo = BufferedInputFile(
                    file=image_data.read(),
                    filename=f"dex_{species.national_dex}.jpg",
                )
                await message.answer_photo(
                    photo=photo, caption=text,
                    reply_markup=keyboard.as_markup() if keyboard else None,
                )
                return
        except Exception as e:
            logger.debug("Dex image fallback to text", error=str(e))

    await message.answer(
        text,
        reply_markup=keyboard.as_markup() if keyboard else None,
    )


# ──────────────────────────────────────────────────────
# Paginated single-entry builder
# ──────────────────────────────────────────────────────

async def _build_entry_page(
    session: AsyncSession,
    species: PokemonSpecies,
    user_id: int,
    page: str,
) -> tuple[str, InlineKeyboardBuilder | None]:
    """Build text + keyboard for a single Pokédex entry page."""
    if page == "details":
        return await _entry_page_details(session, species, user_id)
    elif page == "forms":
        return _entry_page_forms(species)
    else:
        return await _entry_page_overview(session, species, user_id)


def _entry_nav_keyboard(
    species_id: int, current_page: str, has_forms: bool = False
) -> InlineKeyboardBuilder:
    """Build navigation buttons for entry pages."""
    builder = InlineKeyboardBuilder()
    pages = [
        ("📋 Overview", "overview"),
        ("📊 Details", "details"),
    ]
    if has_forms:
        pages.append(("🔥 Forms", "forms"))

    for label, page_key in pages:
        if page_key == current_page:
            builder.button(text=f"[{label}]", callback_data="dex:noop")
        else:
            builder.button(
                text=label,
                callback_data=f"dexentry:{species_id}:{page_key}",
            )
    builder.adjust(len(pages))
    return builder


async def _entry_page_overview(
    session: AsyncSession, species: PokemonSpecies, user_id: int
) -> tuple[str, InlineKeyboardBuilder]:
    """Page 1: Overview — type, stats, evolution, your Pokemon."""
    # Types
    t1 = species.type1
    types = f"{type_emoji(t1)} {t1.title()}"
    if species.type2:
        t2 = species.type2
        types += f" / {type_emoji(t2)} {t2.title()}"

    # Rarity
    rarity = species.rarity.title() if species.rarity else "Common"
    if species.is_legendary:
        rarity = "🌟 Legendary"
    elif species.is_mythical:
        rarity = "💫 Mythical"

    gen_name = GEN_NAMES.get(species.generation, "???")

    # Stats
    stats_line = (
        f"HP: {species.base_hp} | ATK: {species.base_attack} | DEF: {species.base_defense}\n"
        f"SpA: {species.base_sp_attack} | SpD: {species.base_sp_defense} | SPE: {species.base_speed}"
    )

    # Evolution
    evo_line = await _build_evolution_chain_text(session, species)

    # User status
    entry_result = await session.execute(
        select(PokedexEntry)
        .where(PokedexEntry.user_id == user_id)
        .where(PokedexEntry.species_id == species.national_dex)
    )
    entry = entry_result.scalar_one_or_none()
    caught = entry.caught if entry else False
    seen = entry.seen if entry else False
    caught_shiny = entry.caught_shiny if entry else False
    times_caught = entry.times_caught if entry else 0

    if caught:
        status = f"✅ Caught (×{times_caught})"
        if caught_shiny:
            status += " ✨"
    elif seen:
        status = "👁️ Seen"
    else:
        status = "❓ Not encountered"

    # User's Pokemon
    pokemon_result = await session.execute(
        select(Pokemon)
        .where(Pokemon.owner_id == user_id)
        .where(Pokemon.species_id == species.national_dex)
        .order_by(Pokemon.level.desc())
        .limit(5)
    )
    user_pokemon = list(pokemon_result.scalars().all())
    if user_pokemon:
        owned_lines = []
        for poke in user_pokemon:
            shiny = " ✨" if poke.is_shiny else ""
            owned_lines.append(
                f"  Lv.{poke.level} | IV: {poke.iv_percentage:.1f}% | {poke.nature.title()}{shiny}"
            )
        owned_text = "\n".join(owned_lines)
    else:
        owned_text = "  <i>None</i>"

    sprite = poke_emoji(species.national_dex)
    text = (
        f"📕 <b>Pokédex #{species.national_dex:03d} — {sprite}{species.name}</b>\n"
        f"Type: {types}  |  Gen {species.generation} ({gen_name})\n"
        f"Rarity: {rarity}\n\n"
        f"<b>Base Stats</b> (BST: {species.base_stat_total})\n{stats_line}\n\n"
        f"{evo_line}\n\n"
        f"<b>Status:</b> {status}\n"
        f"<b>Your {species.name}:</b>\n{owned_text}"
    )

    has_forms = bool(get_mega_forms(species.national_dex))
    keyboard = _entry_nav_keyboard(species.national_dex, "overview", has_forms)
    return text, keyboard


async def _entry_page_details(
    session: AsyncSession, species: PokemonSpecies, user_id: int
) -> tuple[str, InlineKeyboardBuilder]:
    """Page 2: Details — abilities, gender, egg groups, catch rate."""
    height_m = species.height / 10
    weight_kg = species.weight / 10

    ability_parts = [a.replace("-", " ").title() for a in (species.abilities or [])]
    if species.hidden_ability:
        ability_parts.append(f"{species.hidden_ability.replace('-', ' ').title()} (Hidden)")
    abilities_line = ", ".join(ability_parts) if ability_parts else "Unknown"

    if species.gender_ratio is None:
        gender_line = "Genderless"
    else:
        female = species.gender_ratio
        male = 100 - female
        gender_line = f"♂ {male:.0f}% / ♀ {female:.0f}%"

    egg_groups = [eg.replace("-", " ").title() for eg in (species.egg_groups or [])]
    egg_line = " / ".join(egg_groups) if egg_groups else "Undiscovered"

    catch_pct = round(species.catch_rate / 255 * 100, 1)

    # First caught date
    entry_result = await session.execute(
        select(PokedexEntry)
        .where(PokedexEntry.user_id == user_id)
        .where(PokedexEntry.species_id == species.national_dex)
    )
    entry = entry_result.scalar_one_or_none()
    first_caught = entry.first_caught_at if entry else None
    first_caught_text = first_caught.strftime("%Y-%m-%d") if first_caught else "—"

    text = (
        f"📕 <b>#{species.national_dex:03d} — {species.name} (Details)</b>\n\n"
        f"<b>Height:</b> {height_m:.1f} m\n"
        f"<b>Weight:</b> {weight_kg:.1f} kg\n"
        f"<b>Abilities:</b> {abilities_line}\n"
        f"<b>Gender:</b> {gender_line}\n"
        f"<b>Egg Groups:</b> {egg_line}\n"
        f"<b>Catch Rate:</b> {species.catch_rate} ({catch_pct}%)\n"
        f"<b>First Caught:</b> {first_caught_text}"
    )

    has_forms = bool(get_mega_forms(species.national_dex))
    keyboard = _entry_nav_keyboard(species.national_dex, "details", has_forms)
    return text, keyboard


def _entry_page_forms(
    species: PokemonSpecies,
) -> tuple[str, InlineKeyboardBuilder]:
    """Page 3: Forms — Mega evolutions (and future regional forms)."""
    mega_forms = get_mega_forms(species.national_dex)

    lines = [f"📕 <b>#{species.national_dex:03d} — {species.name} (Forms)</b>\n"]

    if mega_forms:
        for mf in mega_forms:
            types = mf.type1.title()
            if mf.type2:
                types += f" / {mf.type2.title()}"
            bst = mf.base_hp + mf.base_attack + mf.base_defense + mf.base_sp_attack + mf.base_sp_defense + mf.base_speed
            stone_text = f"  Stone: {mf.mega_stone_display}" if mf.mega_stone_display else ""
            lines.append(
                f"🔥 <b>{mf.form_name}</b>\n"
                f"  Type: {types}\n"
                f"  Ability: {mf.ability}\n"
                f"  Stats (BST: {bst})\n"
                f"  HP: {mf.base_hp} | ATK: {mf.base_attack} | DEF: {mf.base_defense}\n"
                f"  SpA: {mf.base_sp_attack} | SpD: {mf.base_sp_defense} | SPE: {mf.base_speed}\n"
                f"{stone_text}"
            )
    else:
        lines.append("<i>No alternate forms available.</i>")

    keyboard = _entry_nav_keyboard(species.national_dex, "forms", has_forms=True)
    return "\n".join(lines), keyboard


@router.callback_query(F.data.startswith("dexentry:"))
async def handle_dexentry_callback(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle pagination between entry pages (overview/details/forms)."""
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Invalid callback")
        return

    try:
        species_id = int(parts[1])
    except ValueError:
        await callback.answer("Invalid species")
        return

    page = parts[2]
    if page not in ("overview", "details", "forms"):
        await callback.answer("Invalid page")
        return

    result = await session.execute(
        select(PokemonSpecies).where(PokemonSpecies.national_dex == species_id)
    )
    species = result.scalar_one_or_none()
    if not species:
        await callback.answer("Species not found")
        return

    text, keyboard = await _build_entry_page(session, species, user.telegram_id, page)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=keyboard.as_markup() if keyboard else None,
        )
    except Exception:
        # If message has a photo, edit_caption instead
        try:
            await callback.message.edit_caption(
                caption=text,
                reply_markup=keyboard.as_markup() if keyboard else None,
            )
        except Exception as e:
            logger.debug("Failed to edit dex entry message", error=str(e))

    await callback.answer()


async def pokedex_help(message: Message) -> None:
    """Show pokedex help."""
    await message.answer(
        "📕 <b>Pokédex Commands</b>\n\n"
        "<b>Overview:</b>\n"
        "/pokedex - Show completion overview\n"
        "/pokedex gen:N - Overview for generation N\n\n"
        "<b>Browse:</b>\n"
        "/pokedex list - All entries\n"
        "/pokedex caught - Caught Pokemon\n"
        "/pokedex missing - Uncaught Pokemon\n"
        "/pokedex shiny - Shinies obtained\n"
        "/pokedex seen - Seen but uncaught\n\n"
        "<b>Filters:</b>\n"
        "gen:N - Filter by generation (1-9)\n"
        "  e.g. /pokedex list gen:3\n"
        "  e.g. /pokedex missing gen:1\n\n"
        "<b>Search:</b>\n"
        "/pokedex [name] - Look up by name\n"
        "/pokedex [number] - Look up by Dex #\n"
        "/pokedex search [query]\n\n"
        "<b>Generations:</b>\n"
        "1: Kanto (151) | 2: Johto (100) | 3: Hoenn (135)\n"
        "4: Sinnoh (107) | 5: Unova (156) | 6: Kalos (72)\n"
        "7: Alola (88) | 8: Galar (96) | 9: Paldea (120)\n\n"
        "<b>Legend:</b>\n"
        "✅ Caught | ✨ Shiny | 👁️ Seen | ❓ Unknown"
    )


@router.callback_query(F.data.startswith("dex:"))
async def handle_pokedex_callback(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle pokedex pagination and filter callbacks."""
    data = callback.data.split(":")

    if len(data) < 2:
        await callback.answer("Invalid callback")
        return

    action = data[1]

    if action == "noop":
        await callback.answer()
        return

    if action == "page":
        page = int(data[2]) if len(data) > 2 else 1
        filter_type = data[3] if len(data) > 3 else "all"
        gen_str = data[4] if len(data) > 4 else "0"
        gen = int(gen_str) if gen_str != "0" else None

        entries, total_count = await get_pokedex_entries(
            session, user.telegram_id, page=page, filter_type=filter_type, gen=gen
        )

        if not entries:
            await callback.answer("No entries on this page")
            return

        total_pages = math.ceil(total_count / ENTRIES_PER_PAGE)
        text = format_pokedex_list_text(entries, total_count, filter_type, gen)
        keyboard = build_pokedex_keyboard(page, total_pages, filter_type, gen)

        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        await callback.answer()

    elif action == "filter":
        filter_type = data[2] if len(data) > 2 else "all"
        page = int(data[3]) if len(data) > 3 else 1
        gen_str = data[4] if len(data) > 4 else "0"
        gen = int(gen_str) if gen_str != "0" else None

        entries, total_count = await get_pokedex_entries(
            session, user.telegram_id, page=page, filter_type=filter_type, gen=gen
        )

        if not entries:
            filter_names = {
                "all": "entries",
                "caught": "caught Pokemon",
                "missing": "missing Pokemon",
                "shiny": "shiny Pokemon",
                "seen": "seen (but uncaught) Pokemon",
            }
            await callback.answer(f"No {filter_names.get(filter_type, 'entries')} found!")
            return

        total_pages = math.ceil(total_count / ENTRIES_PER_PAGE)
        text = format_pokedex_list_text(entries, total_count, filter_type, gen)
        keyboard = build_pokedex_keyboard(page, total_pages, filter_type, gen)

        filter_titles = {
            "all": "All Pokemon",
            "caught": "Caught Pokemon",
            "missing": "Missing Pokemon",
            "shiny": "Shiny Pokemon",
            "seen": "Seen (Uncaught)",
        }

        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
        await callback.answer(f"Showing {filter_titles.get(filter_type, 'all')}")
