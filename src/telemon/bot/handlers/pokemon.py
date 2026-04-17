"""Pokemon collection handlers."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.config import CURRENCY_SHORT
from telemon.core.constants import MAX_FRIENDSHIP
from telemon.core.evolution import check_evolution, evolve_pokemon, get_possible_evolutions
from telemon.database.models import Pokemon, PokemonSpecies, User
from telemon.logging import get_logger

router = Router(name="pokemon")
logger = get_logger(__name__)

POKEMON_PER_PAGE = 15


def parse_pokemon_args(text: str) -> dict:
    """Parse filter and sort arguments from command.
    
    Supports both formats:
      /pokemon --shiny --type fire --order iv
      /pokemon shiny type:fire sort:iv gen:3 name:char
    """
    args = {
        "shiny": False,
        "legendary": False,
        "mythical": False,
        "favorites": False,
        "name": None,
        "type": None,
        "gen": None,
        "order": "recent",
        "page": 1,
    }

    if not text:
        return args

    parts = text.split()
    i = 0
    while i < len(parts):
        part = parts[i].lower()

        # Flag-style arguments
        if part in ("--shiny", "shiny"):
            args["shiny"] = True
        elif part in ("--legendary", "legendary", "leg"):
            args["legendary"] = True
        elif part in ("--mythical", "mythical", "myth"):
            args["mythical"] = True
        elif part in ("--favorites", "--fav", "favorites", "fav"):
            args["favorites"] = True

        # Key:value style arguments
        elif ":" in part:
            key, _, value = part.partition(":")
            if key in ("type", "t") and value:
                args["type"] = value
            elif key in ("gen", "g", "generation") and value.isdigit():
                args["gen"] = int(value)
            elif key in ("name", "n") and value:
                args["name"] = value
            elif key in ("sort", "order", "o") and value:
                args["order"] = value
            elif key in ("page", "p") and value.isdigit():
                args["page"] = int(value)

        # Legacy --key value style
        elif part == "--name" and i + 1 < len(parts):
            i += 1
            args["name"] = parts[i].lower()
        elif part == "--type" and i + 1 < len(parts):
            i += 1
            args["type"] = parts[i].lower()
        elif part == "--order" and i + 1 < len(parts):
            i += 1
            args["order"] = parts[i].lower()
        elif part == "--gen" and i + 1 < len(parts):
            i += 1
            if parts[i].isdigit():
                args["gen"] = int(parts[i])

        # Page number as plain digit
        elif part.isdigit():
            args["page"] = int(part)

        i += 1

    return args


async def get_user_pokemon_by_index(
    session: AsyncSession, user_id: int, index: int
) -> Pokemon | None:
    """Get a user's Pokemon by 1-based index (ordered by catch date asc — first caught = #1)."""
    result = await session.execute(
        select(Pokemon)
        .where(Pokemon.owner_id == user_id)
        .order_by(Pokemon.caught_at.asc())
        .offset(index - 1)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def resolve_pokemon(
    session: AsyncSession, user: User, arg: str | None
) -> Pokemon | None:
    """Resolve a Pokemon from argument (index number, 'l'/'latest', or '0') or selected Pokemon.

    Shortcuts:
        0        → selected Pokemon
        l        → most recently caught Pokemon
        latest   → most recently caught Pokemon
        -l       → most recently caught Pokemon
        --latest → most recently caught Pokemon
        <number> → Pokemon by collection index
    """
    LATEST_ALIASES = {"l", "-l", "--latest", "-latest", "latest"}

    if arg:
        if arg == "0":
            # Explicit selected Pokemon
            pass  # Fall through to selected Pokemon logic below
        elif arg.lower() in LATEST_ALIASES:
            # Latest = most recently caught, query desc + limit 1
            result = await session.execute(
                select(Pokemon)
                .where(Pokemon.owner_id == user.telegram_id)
                .order_by(Pokemon.caught_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()
        elif arg.isdigit():
            return await get_user_pokemon_by_index(session, user.telegram_id, int(arg))
    
    # Fall back to selected Pokemon
    if user.selected_pokemon_id:
        result = await session.execute(
            select(Pokemon)
            .where(Pokemon.id == user.selected_pokemon_id)
            .where(Pokemon.owner_id == user.telegram_id)
        )
        return result.scalar_one_or_none()
    
    return None


@router.message(Command("pokemon", "p"))
async def cmd_pokemon(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /pokemon command to list user's Pokemon."""
    # Parse arguments
    text = message.text or ""
    args = parse_pokemon_args(text.split(maxsplit=1)[1] if " " in text else "")

    # Build query
    query = (
        select(Pokemon)
        .where(Pokemon.owner_id == user.telegram_id)
        .join(PokemonSpecies, Pokemon.species_id == PokemonSpecies.national_dex)
    )

    # Apply filters
    if args["shiny"]:
        query = query.where(Pokemon.is_shiny == True)
    if args["legendary"]:
        query = query.where(PokemonSpecies.is_legendary == True)
    if args["mythical"]:
        query = query.where(PokemonSpecies.is_mythical == True)
    if args["favorites"]:
        query = query.where(Pokemon.is_favorite == True)
    if args["name"]:
        query = query.where(PokemonSpecies.name_lower.contains(args["name"]))
    if args["type"]:
        query = query.where(
            (PokemonSpecies.type1 == args["type"]) | (PokemonSpecies.type2 == args["type"])
        )
    if args["gen"]:
        query = query.where(PokemonSpecies.generation == args["gen"])

    # Apply sorting
    order = args["order"]
    if order == "iv":
        query = query.order_by(
            (
                Pokemon.iv_hp
                + Pokemon.iv_attack
                + Pokemon.iv_defense
                + Pokemon.iv_sp_attack
                + Pokemon.iv_sp_defense
                + Pokemon.iv_speed
            ).desc()
        )
    elif order == "level":
        query = query.order_by(Pokemon.level.desc())
    elif order == "dex":
        query = query.order_by(PokemonSpecies.national_dex.asc())
    elif order == "name":
        query = query.order_by(PokemonSpecies.name.asc())
    else:  # recent (default)
        query = query.order_by(Pokemon.caught_at.asc())

    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await session.execute(count_query)
    total_count = total_result.scalar() or 0

    if total_count == 0:
        has_filter = args["shiny"] or args["legendary"] or args["mythical"] or args["favorites"] or args["name"] or args["type"] or args["gen"]
        if has_filter:
            await message.answer("No Pokemon match your filters.")
        else:
            await message.answer(
                "You don't have any Pokemon yet!\n"
                "Catch some in group chats with /catch"
            )
        return

    # Paginate
    page = args["page"]
    total_pages = (total_count + POKEMON_PER_PAGE - 1) // POKEMON_PER_PAGE
    page = max(1, min(page, total_pages))
    offset = (page - 1) * POKEMON_PER_PAGE
    query = query.offset(offset).limit(POKEMON_PER_PAGE)

    result = await session.execute(query)
    pokemon_list = result.scalars().all()

    # Build active filter description
    filter_parts = []
    if args["shiny"]:
        filter_parts.append("shiny")
    if args["legendary"]:
        filter_parts.append("legendary")
    if args["mythical"]:
        filter_parts.append("mythical")
    if args["favorites"]:
        filter_parts.append("favorites")
    if args["type"]:
        filter_parts.append(f"type:{args['type']}")
    if args["gen"]:
        filter_parts.append(f"gen:{args['gen']}")
    if args["name"]:
        filter_parts.append(f"name:{args['name']}")
    
    filter_text = f" [{', '.join(filter_parts)}]" if filter_parts else ""
    sort_text = f" sorted by {order}" if order != "recent" else ""

    # Determine if any filters are active
    has_filter = (
        args["shiny"] or args["legendary"] or args["mythical"]
        or args["favorites"] or args["name"] or args["type"] or args["gen"]
    )

    # When filters are active, compute real inventory indices via ROW_NUMBER()
    real_indices: dict[str, int] = {}
    if has_filter and pokemon_list:
        row_num_sq = (
            select(
                Pokemon.id,
                func.row_number()
                .over(order_by=Pokemon.caught_at.asc())
                .label("inv_idx"),
            )
            .where(Pokemon.owner_id == user.telegram_id)
            .subquery()
        )
        poke_ids = [p.id for p in pokemon_list]
        idx_result = await session.execute(
            select(row_num_sq.c.id, row_num_sq.c.inv_idx).where(
                row_num_sq.c.id.in_(poke_ids)
            )
        )
        real_indices = {str(row.id): int(row.inv_idx) for row in idx_result}

    # Build response
    lines = [f"<b>Your Pokemon</b> ({total_count} total){filter_text}{sort_text}\n"]

    for i, poke in enumerate(pokemon_list):
        # Use real inventory index when filters are active, else sequential
        if has_filter:
            idx = real_indices.get(str(poke.id), offset + i + 1)
        else:
            idx = offset + i + 1
        shiny = "✨ " if poke.is_shiny else ""
        fav = "❤️ " if poke.is_favorite else ""
        
        # Show species name + nickname if nicknamed
        if poke.nickname:
            name = f"{poke.nickname} ({poke.species.name})"
        else:
            name = poke.species.name
        
        iv_pct = poke.iv_percentage
        
        # Selected indicator
        selected = " ◀️" if str(poke.id) == user.selected_pokemon_id else ""

        lines.append(
            f"{idx}. {shiny}{fav}<b>{name}</b> "
            f"Lv.{poke.level} | {iv_pct}% IV{selected}"
        )

    # Pagination info
    if total_pages > 1:
        lines.append(f"\nPage {page}/{total_pages}")
        lines.append(f"<i>Use /pokemon {page + 1} for next page</i>" if page < total_pages else "")

    # Build pagination keyboard
    builder = InlineKeyboardBuilder()
    if page > 1:
        builder.button(text="◀️ Prev", callback_data=f"pokemon:page:{page - 1}")
    if page < total_pages:
        builder.button(text="Next ▶️", callback_data=f"pokemon:page:{page + 1}")
    builder.adjust(2)

    await message.answer("\n".join(lines), reply_markup=builder.as_markup() if total_pages > 1 else None)


@router.message(Command("info", "i"))
async def cmd_info(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /info command to show Pokemon details."""
    text = message.text or ""
    args = text.split()
    
    arg = args[1] if len(args) >= 2 else None
    poke = await resolve_pokemon(session, user, arg)

    if not poke:
        await message.answer(
            "Pokemon not found!\n"
            "Usage: /info [number] or select one with /select"
        )
        return

    # Build detailed info
    shiny = " ✨ SHINY" if poke.is_shiny else ""
    fav = " ❤️" if poke.is_favorite else ""

    type_text = poke.species.type1.capitalize()
    if poke.species.type2:
        type_text += f" / {poke.species.type2.capitalize()}"

    gender_text = ""
    if poke.gender == "male":
        gender_text = " ♂"
    elif poke.gender == "female":
        gender_text = " ♀"

    # IV quality
    iv_pct = poke.iv_percentage
    if iv_pct >= 90:
        iv_rating = "⭐ Amazing"
    elif iv_pct >= 75:
        iv_rating = "Great"
    elif iv_pct >= 50:
        iv_rating = "Good"
    elif iv_pct >= 25:
        iv_rating = "Average"
    else:
        iv_rating = "Poor"

    nickname_line = f'\nNickname: "{poke.nickname}"' if poke.nickname else ""

    info = f"""\
<b>{poke.display_name}</b>{shiny}{fav}{gender_text}
{poke.species.name} #{poke.species.national_dex} | Gen {poke.species.generation}{nickname_line}

<b>Type:</b> {type_text}
<b>Level:</b> {poke.level}
<b>Nature:</b> {poke.nature.capitalize()}
<b>Ability:</b> {poke.ability or "Unknown"}

<b>IVs</b> ({iv_pct}% - {iv_rating})
HP: {poke.iv_hp} | Atk: {poke.iv_attack} | Def: {poke.iv_defense}
SpA: {poke.iv_sp_attack} | SpD: {poke.iv_sp_defense} | Spe: {poke.iv_speed}

<b>Held Item:</b> {poke.held_item or 'None'}
<b>Friendship:</b> {poke.friendship}/{MAX_FRIENDSHIP}

<i>Caught {poke.caught_at.strftime('%Y-%m-%d')}</i>"""

    # Try to send with Pokemon artwork image
    try:
        from aiogram.types import BufferedInputFile
        from telemon.core.imaging import generate_spawn_image

        image_data = await generate_spawn_image(
            dex_number=poke.species.national_dex,
            primary_type=poke.species.type1 or "normal",
            shiny=poke.is_shiny,
        )
        if image_data:
            photo = BufferedInputFile(
                file=image_data.read(),
                filename=f"info_{poke.species.national_dex}.jpg",
            )
            await message.answer_photo(photo=photo, caption=info)
            return
    except Exception:
        pass  # Fall back to text-only

    await message.answer(info)


@router.message(Command("select", "sel"))
async def cmd_select(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /select command to set active Pokemon."""
    text = message.text or ""
    args = text.split()

    if len(args) < 2:
        await message.answer("Usage: /select [number|l]\nExample: /select 1 or /select l")
        return

    arg = args[1]
    poke = await resolve_pokemon(session, user, arg)

    if not poke:
        await message.answer("Pokemon not found! Check your list with /pokemon")
        return

    user.selected_pokemon_id = str(poke.id)
    await session.commit()

    shiny = " ✨" if poke.is_shiny else ""
    await message.answer(
        f"Selected <b>{poke.display_name}</b>{shiny} (Lv.{poke.level}) as your active Pokemon!"
    )


@router.message(Command("nickname", "nick"))
async def cmd_nickname(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /nickname command to rename selected Pokemon."""
    text = message.text or ""
    args = text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer(
            "<b>Usage:</b>\n"
            "/nickname [new name] - Rename selected Pokemon\n"
            "/nickname clear - Remove nickname\n\n"
            "Select a Pokemon first with /select [number]"
        )
        return

    new_name = args[1].strip()

    # Get selected Pokemon
    poke = await resolve_pokemon(session, user, None)
    if not poke:
        await message.answer("No Pokemon selected! Use /select [number] first.")
        return

    if new_name.lower() == "clear":
        old_name = poke.nickname or poke.species.name
        poke.nickname = None
        await session.commit()
        await message.answer(f"Cleared nickname for <b>{poke.species.name}</b>!")
        return

    # Validate nickname
    if len(new_name) > 30:
        await message.answer("Nickname is too long! Max 30 characters.")
        return

    if len(new_name) < 1:
        await message.answer("Nickname cannot be empty!")
        return

    old_name = poke.display_name
    poke.nickname = new_name[:30]
    await session.commit()

    await message.answer(
        f"Renamed <b>{old_name}</b> to <b>{new_name}</b>! ({poke.species.name})"
    )


@router.message(Command("favorite", "fav"))
async def cmd_favorite(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /favorite command to toggle favorite status."""
    text = message.text or ""
    args = text.split()

    arg = args[1] if len(args) >= 2 else None
    poke = await resolve_pokemon(session, user, arg)

    if not poke:
        await message.answer(
            "Pokemon not found!\n"
            "Usage: /fav [number] or select one with /select first"
        )
        return

    poke.is_favorite = not poke.is_favorite
    await session.commit()

    if poke.is_favorite:
        await message.answer(f"❤️ <b>{poke.display_name}</b> is now a favorite!")
    else:
        await message.answer(f"<b>{poke.display_name}</b> removed from favorites.")


@router.message(Command("release"))
async def cmd_release(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /release command to release Pokemon (single, duplicates, or filtered)."""
    text = message.text or ""
    args = text.split()

    # Subcommands: /release duplicates, /release all [filters]
    if len(args) >= 2 and args[1].lower() in ("duplicates", "dups"):
        await release_duplicates(message, session, user)
        return

    if len(args) >= 2 and args[1].lower() == "all":
        # Parse filters from remaining args
        filter_text = " ".join(args[2:]) if len(args) > 2 else ""
        await release_filtered(message, session, user, filter_text)
        return

    arg = args[1] if len(args) >= 2 else None
    poke = await resolve_pokemon(session, user, arg)

    if not poke:
        await message.answer(
            "Pokemon not found!\n"
            "Usage: /release [number]"
        )
        return

    if not poke.is_releasable:
        if poke.is_favorite:
            await message.answer(
                "This Pokemon is a favorite! "
                "Remove from favorites first with /fav"
            )
        else:
            await message.answer("This Pokemon cannot be released right now.")
        return

    # Build confirmation keyboard
    builder = InlineKeyboardBuilder()
    builder.button(text="Yes, release", callback_data=f"release:confirm:{poke.id}")
    builder.button(text="Cancel", callback_data="release:cancel")
    builder.adjust(2)

    await message.answer(
        f"Are you sure you want to release <b>{poke.display_name}</b> "
        f"(Lv.{poke.level}, {poke.iv_percentage}% IV)?\n"
        "This cannot be undone!",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("release:"))
async def callback_release(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle release confirmation callbacks."""
    if not callback.data:
        return

    action = callback.data.split(":")[1]

    if action == "cancel":
        await callback.message.edit_text("Release cancelled.")
        await callback.answer()
        return

    if action == "confirm":
        pokemon_id = callback.data.split(":")[2]

        result = await session.execute(
            select(Pokemon)
            .where(Pokemon.owner_id == user.telegram_id)
            .where(Pokemon.id == pokemon_id)
        )
        poke = result.scalar_one_or_none()

        if not poke or not poke.is_releasable:
            await callback.message.edit_text("Pokemon not found or cannot be released.")
            await callback.answer()
            return

        name = poke.display_name
        await session.delete(poke)
        await session.commit()

        await callback.message.edit_text(f"Goodbye, <b>{name}</b>...")
        await callback.answer("Released!")


@router.callback_query(lambda c: c.data and c.data.startswith("pokemon:page:"))
async def callback_pokemon_page(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle Pokemon list pagination callbacks."""
    if not callback.data:
        return

    page = int(callback.data.split(":")[2])

    # Rebuild the query (default filters, just pagination)
    query = (
        select(Pokemon)
        .where(Pokemon.owner_id == user.telegram_id)
        .join(PokemonSpecies, Pokemon.species_id == PokemonSpecies.national_dex)
        .order_by(Pokemon.caught_at.asc())
    )

    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await session.execute(count_query)
    total_count = total_result.scalar() or 0

    total_pages = (total_count + POKEMON_PER_PAGE - 1) // POKEMON_PER_PAGE
    page = max(1, min(page, total_pages))
    offset = (page - 1) * POKEMON_PER_PAGE

    result = await session.execute(query.offset(offset).limit(POKEMON_PER_PAGE))
    pokemon_list = result.scalars().all()

    lines = [f"<b>Your Pokemon</b> ({total_count} total)\n"]

    for i, poke in enumerate(pokemon_list):
        idx = offset + i + 1
        shiny = "✨ " if poke.is_shiny else ""
        fav = "❤️ " if poke.is_favorite else ""
        
        if poke.nickname:
            name = f"{poke.nickname} ({poke.species.name})"
        else:
            name = poke.species.name

        iv_pct = poke.iv_percentage
        selected = " ◀️" if str(poke.id) == user.selected_pokemon_id else ""

        lines.append(
            f"{idx}. {shiny}{fav}<b>{name}</b> "
            f"Lv.{poke.level} | {iv_pct}% IV{selected}"
        )

    if total_pages > 1:
        lines.append(f"\nPage {page}/{total_pages}")

    builder = InlineKeyboardBuilder()
    if page > 1:
        builder.button(text="◀️ Prev", callback_data=f"pokemon:page:{page - 1}")
    if page < total_pages:
        builder.button(text="Next ▶️", callback_data=f"pokemon:page:{page + 1}")
    builder.adjust(2)

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=builder.as_markup() if total_pages > 1 else None,
    )
    await callback.answer()


@router.message(Command("evolve"))
async def cmd_evolve(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /evolve command to evolve a Pokemon."""
    text = message.text or ""
    args = text.split()

    # Get Pokemon list for user
    result = await session.execute(
        select(Pokemon)
        .where(Pokemon.owner_id == user.telegram_id)
        .order_by(Pokemon.caught_at.asc())
    )
    pokemon_list = result.scalars().all()

    if not pokemon_list:
        await message.answer("You don't have any Pokemon!")
        return

    # Parse Pokemon target and optional item
    LATEST_ALIASES = {"l", "-l", "--latest", "-latest", "latest"}
    poke = None
    item_name = None

    if len(args) >= 2:
        first_arg = args[1]
        if first_arg.isdigit() or first_arg == "0" or first_arg.lower() in LATEST_ALIASES:
            # First arg is a Pokemon target
            poke = await resolve_pokemon(session, user, first_arg)
            if len(args) >= 3:
                item_name = " ".join(args[2:])
        else:
            # No index specified, use selected pokemon
            item_name = " ".join(args[1:])

    # Fall back to selected pokemon if no target resolved yet
    if poke is None:
        poke = await resolve_pokemon(session, user, None)

    if not poke:
        await message.answer(
            "Please specify a Pokemon to evolve!\n"
            "Usage: /evolve [number|l] [item name]\n"
            "Example: /evolve 1\n"
            "Example: /evolve l fire stone"
        )
        return

    # Check evolution possibilities
    evo_result = await check_evolution(
        session, poke, user.telegram_id, use_item=item_name
    )

    if evo_result.can_evolve:
        # Attempt evolution
        success, message_text = await evolve_pokemon(
            session, poke, user.telegram_id, use_item=item_name
        )

        if success:
            # Refresh the Pokemon data
            await session.refresh(poke)

            # Update quest progress
            from telemon.core.quests import update_quest_progress
            completed = await update_quest_progress(session, user.telegram_id, "evolve")
            quest_text = ""
            if completed:
                await session.commit()
                for q in completed:
                    quest_text += f"\n📋 Quest complete: {q.description} (+{q.reward_coins:,} {CURRENCY_SHORT})"
            
            # Achievement hooks
            user.total_evolutions += 1
            from telemon.core.achievements import check_achievements, format_achievement_notification
            new_achs = await check_achievements(session, user.telegram_id, "evolve")
            ach_text = format_achievement_notification(new_achs)
            if new_achs:
                await session.commit()

            # Team XP hook for evolution
            team_lvl_text = ""
            try:
                if user.team_id:
                    from telemon.core.teams import add_team_xp
                    xp_added, new_lvl, leveled = await add_team_xp(session, user.team_id, "evolve")
                    if leveled:
                        team_lvl_text = f"\nYour team leveled up to Lv.{new_lvl}!"
            except Exception:
                pass

            await message.answer(
                f"<b>Congratulations!</b>\n\n"
                f"{message_text}\n\n"
                f"Your Pokemon is now a <b>{poke.species.name}</b>!{quest_text}{ach_text}{team_lvl_text}"
            )
            
            logger.info(
                "Pokemon evolved successfully",
                user_id=user.telegram_id,
                pokemon_id=str(poke.id),
                new_species=poke.species.name,
            )
        else:
            await message.answer(f"Evolution failed: {message_text}")
    else:
        # Show evolution requirements
        evolutions = get_possible_evolutions(poke.species_id)
        
        if not evolutions:
            await message.answer(
                f"<b>{poke.species.name}</b> cannot evolve."
            )
            return

        lines = [f"<b>{poke.species.name}</b> Evolution Info\n"]
        
        if evo_result.evolved_species_name:
            lines.append(f"Evolves to: <b>{evo_result.evolved_species_name}</b>")
            lines.append(f"Requirement: {evo_result.requirement}")
            lines.append(f"\n{evo_result.missing_requirement}")
        else:
            lines.append(evo_result.missing_requirement or "Cannot evolve.")

        # Show available evolutions
        if len(evolutions) > 1:
            lines.append("\n<b>Possible Evolutions:</b>")
            for evo in evolutions:
                trigger = evo["trigger"]
                if trigger == "level":
                    lines.append(f"  Level {evo.get('min_level', '?')}+")
                elif trigger == "item":
                    lines.append(f"  {evo.get('item', 'Unknown item').title()}")
                elif trigger == "trade":
                    lines.append(f"  Trade")
                elif trigger == "friendship":
                    lines.append(f"  High Friendship")

        await message.answer("\n".join(lines))


async def release_duplicates(
    message: Message, session: AsyncSession, user: User
) -> None:
    """Release duplicate Pokemon, keeping the best IV of each species.

    Skips favorites, selected, on-market, and in-trade Pokemon.
    """
    result = await session.execute(
        select(Pokemon)
        .where(Pokemon.owner_id == user.telegram_id)
        .order_by(Pokemon.species_id.asc())
    )
    all_pokemon = list(result.scalars().all())

    if not all_pokemon:
        await message.answer("You don't have any Pokemon!")
        return

    # Group by species_id
    from collections import defaultdict
    species_groups: dict[int, list[Pokemon]] = defaultdict(list)
    for poke in all_pokemon:
        species_groups[poke.species_id].append(poke)

    to_release: list[Pokemon] = []
    for species_id, pokes in species_groups.items():
        if len(pokes) <= 1:
            continue
        # Sort by IV descending — keep the best
        pokes.sort(key=lambda p: p.iv_percentage, reverse=True)
        for poke in pokes[1:]:  # Skip the best one
            # Skip protected Pokemon
            if poke.is_favorite:
                continue
            if str(poke.id) == user.selected_pokemon_id:
                continue
            if poke.is_on_market:
                continue
            if poke.is_in_trade:
                continue
            if poke.is_shiny:
                continue  # Never auto-release shinies
            to_release.append(poke)

    if not to_release:
        await message.answer(
            "No duplicates to release!\n"
            "(Favorites, shinies, selected, and traded Pokemon are always kept)"
        )
        return

    # Build confirmation
    builder = InlineKeyboardBuilder()
    count = len(to_release)
    builder.button(text=f"Yes, release {count} Pokemon", callback_data=f"bulkrel:dups:{count}")
    builder.button(text="Cancel", callback_data="bulkrel:cancel")
    builder.adjust(1)

    # Store release IDs in memory for the callback
    _pending_bulk_releases[user.telegram_id] = [str(p.id) for p in to_release]

    await message.answer(
        f"<b>Release Duplicates</b>\n\n"
        f"Found <b>{count}</b> duplicate Pokemon to release.\n"
        f"(Keeping best IV of each species)\n\n"
        f"<i>Favorites, shinies, selected, on-market, and in-trade Pokemon are always kept.</i>\n\n"
        f"Are you sure?",
        reply_markup=builder.as_markup(),
    )


async def release_filtered(
    message: Message, session: AsyncSession, user: User, filter_text: str
) -> None:
    """Release Pokemon matching filters. Skips protected Pokemon."""
    if not filter_text:
        await message.answer(
            "<b>Bulk Release</b>\n\n"
            "Usage: /release all [filters]\n\n"
            "<b>Filters:</b>\n"
            "iv:0-50 — Release Pokemon with IV below 50%\n"
            "gen:1 — Release Gen 1 Pokemon\n"
            "type:normal — Release Normal type\n"
            "name:rattata — Release by name\n\n"
            "<b>Examples:</b>\n"
            "/release all iv:0-30\n"
            "/release all gen:1 iv:0-50\n\n"
            "<i>Favorites, shinies, selected Pokemon are always kept.</i>"
        )
        return

    # Parse filters
    fargs = parse_pokemon_args(filter_text)

    # Build query
    query = (
        select(Pokemon)
        .where(Pokemon.owner_id == user.telegram_id)
        .join(PokemonSpecies, Pokemon.species_id == PokemonSpecies.national_dex)
    )

    # Must have at least one filter to prevent accidental mass release
    has_filter = False

    if fargs["type"]:
        query = query.where(
            (PokemonSpecies.type1 == fargs["type"]) | (PokemonSpecies.type2 == fargs["type"])
        )
        has_filter = True
    if fargs["gen"]:
        query = query.where(PokemonSpecies.generation == fargs["gen"])
        has_filter = True
    if fargs["name"]:
        query = query.where(PokemonSpecies.name_lower.contains(fargs["name"]))
        has_filter = True

    if not has_filter:
        await message.answer("You must specify at least one filter (type:, gen:, name:, etc.).")
        return

    result = await session.execute(query)
    candidates = list(result.scalars().all())

    # Apply IV filter manually
    to_release: list[Pokemon] = []
    for poke in candidates:
        # Skip protected
        if poke.is_favorite:
            continue
        if str(poke.id) == user.selected_pokemon_id:
            continue
        if poke.is_on_market:
            continue
        if poke.is_in_trade:
            continue
        if poke.is_shiny:
            continue
        to_release.append(poke)

    if not to_release:
        await message.answer("No Pokemon match your filters (after excluding protected Pokemon).")
        return

    count = len(to_release)
    builder = InlineKeyboardBuilder()
    builder.button(text=f"Yes, release {count} Pokemon", callback_data=f"bulkrel:filt:{count}")
    builder.button(text="Cancel", callback_data="bulkrel:cancel")
    builder.adjust(1)

    _pending_bulk_releases[user.telegram_id] = [str(p.id) for p in to_release]

    filter_desc = []
    if fargs["type"]:
        filter_desc.append(f"type:{fargs['type']}")
    if fargs["gen"]:
        filter_desc.append(f"gen:{fargs['gen']}")
    if fargs["name"]:
        filter_desc.append(f"name:{fargs['name']}")

    await message.answer(
        f"<b>Bulk Release</b>\n\n"
        f"Found <b>{count}</b> Pokemon matching [{', '.join(filter_desc)}]\n\n"
        f"<i>Favorites, shinies, selected, on-market, and in-trade Pokemon are excluded.</i>\n\n"
        f"Are you sure?",
        reply_markup=builder.as_markup(),
    )


# In-memory store for pending bulk releases
_pending_bulk_releases: dict[int, list[str]] = {}


@router.callback_query(lambda c: c.data and c.data.startswith("bulkrel:"))
async def callback_bulk_release(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    """Handle bulk release confirmation callbacks."""
    if not callback.data:
        return

    action = callback.data.split(":")[1]

    if action == "cancel":
        _pending_bulk_releases.pop(user.telegram_id, None)
        await callback.message.edit_text("Bulk release cancelled.")
        await callback.answer()
        return

    # Get pending release IDs
    pokemon_ids = _pending_bulk_releases.pop(user.telegram_id, None)
    if not pokemon_ids:
        await callback.message.edit_text("No pending release found. Try again.")
        await callback.answer()
        return

    # Delete all matching Pokemon
    released = 0
    for pid in pokemon_ids:
        result = await session.execute(
            select(Pokemon)
            .where(Pokemon.id == pid)
            .where(Pokemon.owner_id == user.telegram_id)
        )
        poke = result.scalar_one_or_none()
        if poke and not poke.is_favorite and not poke.is_shiny:
            await session.delete(poke)
            released += 1

    await session.commit()

    await callback.message.edit_text(
        f"Released <b>{released}</b> Pokemon. Goodbye!"
    )
    await callback.answer(f"Released {released} Pokemon!")
