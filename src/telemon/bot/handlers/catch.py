"""Catching-related handlers."""

from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.config import CURRENCY_NAME, CURRENCY_SHORT
from telemon.core import regional, shiny
from telemon.core.constants import NATURES, MAX_IV, MAX_IV_TOTAL, MAX_LEVEL, MAX_FRIENDSHIP, CATCH_LEVEL_MIN, CATCH_LEVEL_MAX, determine_gender, iv_percentage, random_nature
from telemon.core.emoji import poke_emoji
from telemon.core.shiny import has_shiny_charm
from telemon.database.models import ActiveSpawn, Group, Pokemon, PokedexEntry, User
from telemon.logging import get_logger
from telemon.core.text import esc

router = Router(name="catch")
logger = get_logger(__name__)

# Track last hint time per user per chat (simple in-memory cache)
_hint_cooldowns: dict[tuple[int, int], datetime] = {}
HINT_COOLDOWN_SECONDS = 10
_HINT_COOLDOWN_MAX_SIZE = 500


def generate_hint(name: str, hints_used: int) -> str:
    """Generate a hint showing some letters of the Pokemon name."""
    if hints_used == 0:
        # First hint: show length and first letter
        return f"{name[0]}{'_' * (len(name) - 1)} ({len(name)} letters)"
    elif hints_used == 1:
        # Second hint: show first, last, and some middle letters
        revealed = set([0, len(name) - 1])
        # Reveal ~30% of letters
        import random

        for i in range(1, len(name) - 1):
            if random.random() < 0.3:
                revealed.add(i)
        return "".join(c if i in revealed else "_" for i, c in enumerate(name))
    else:
        # Third hint: show most letters
        import random

        return "".join(c if random.random() < 0.7 else "_" for c in name)


@router.message(Command("catch", "c"))
async def cmd_catch(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /catch command."""
    if not message.text:
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(" Please specify the Pokemon name!\nUsage: /catch [name]")
        return

    pokemon_name = args[1].strip().lower()

    # Get active spawn for this chat
    chat_id = message.chat.id
    result = await session.execute(
        select(ActiveSpawn)
        .where(ActiveSpawn.chat_id == chat_id)
        .where(ActiveSpawn.caught_by.is_(None))
        .where(ActiveSpawn.expires_at > datetime.utcnow())
        .order_by(ActiveSpawn.spawned_at.desc())
        .limit(1)
    )
    spawn = result.scalar_one_or_none()

    if not spawn:
        await message.answer(" There's no wild Pokemon here right now!")
        return

    # Check if name matches (exact match, case-insensitive)
    # Hyphens and spaces are stripped for comparison so "hooh" matches "ho-oh"
    # and "mrmime" matches "mr-mime".
    actual_name = spawn.species.name_lower

    def _normalize(n: str) -> str:
        return n.lower().replace("-", "").replace(" ", "").strip()

    # Multi-word base names that must NOT be split when extracting base names.
    # e.g. "mr-mime" is a base name, not a form of "mr".
    _BASE_NAME_WHITELIST = {
        "mr-mime", "mime-jr", "ho-oh", "porygon-z", "type-null",
        "jangmo-o", "hakamo-o", "kommo-o", "nidoran-f", "nidoran-m",
        "tapu-koko", "tapu-lele", "tapu-bulu", "tapu-fini", "mr-rime",
        "chi-yu", "chien-pao", "ting-lu", "wo-chien",
        "great-tusk", "scream-tail", "brute-bonnet", "flutter-mane",
        "slither-wing", "sandy-shocks", "roaring-moon", "walking-wake",
        "gouging-fire", "raging-bolt",
        "iron-treads", "iron-bundle", "iron-hands", "iron-jugulis",
        "iron-moth", "iron-thorns", "iron-valiant", "iron-leaves",
        "iron-boulder", "iron-crown",
    }

    def _get_base_name(name_lower: str) -> str | None:
        """Extract the base Pokemon name from a form name.

        e.g. "urshifu-single-strike" → "urshifu"
             "deoxys-normal" → "deoxys"
             "mr-mime" → None (it's already a base name)
        Returns None if the name is already a base name.
        """
        if name_lower in _BASE_NAME_WHITELIST:
            return None
        if "-" in name_lower:
            return name_lower.split("-")[0]
        if " " in name_lower:
            return name_lower.split(" ")[0]
        return None

    # Check exact match (normalized — strips hyphens/spaces)
    name_matches = _normalize(pokemon_name) == _normalize(actual_name)

    if not name_matches and regional.is_regional(spawn.species_id):
        # Regional forms are named "<region> <base>", so splitting on the space
        # would yield the region ("alolan") as the base name.  Accept the full
        # name, the region-less name ("vulpix"), and any region alias
        # ("alola vulpix"), but never a bare region word.
        form = regional.get_form(spawn.species_id)
        accepted = {_normalize(actual_name), _normalize(form.short_name)}
        # "paldean tauros" for any of the three breeds.
        accepted.add(_normalize(form.short_name.split(" (")[0]))
        for alias, canonical in regional.REGION_ALIASES.items():
            if canonical == form.region:
                accepted.add(_normalize(f"{alias} {form.short_name}"))
                accepted.add(_normalize(f"{alias} {form.short_name.split(' (')[0]}"))
        name_matches = _normalize(pokemon_name) in accepted
    elif not name_matches:
        # Also check base-name match for form Pokemon
        base = _get_base_name(actual_name)
        if base:
            name_matches = _normalize(pokemon_name) == _normalize(base)

    if not name_matches:
        # Wrong name
        await message.answer(
            f" That's not the right Pokemon!\n"
            f"Use /hint if you need help identifying it."
        )
        return

    # Claim the spawn atomically.  The SELECT above only proves the spawn was
    # unclaimed when we read it; two /catch messages arriving together would
    # both pass it and both create a Pokemon.  A conditional UPDATE lets the
    # database pick exactly one winner.
    claim = await session.execute(
        update(ActiveSpawn)
        .where(ActiveSpawn.id == spawn.id)
        .where(ActiveSpawn.caught_by.is_(None))
        .values(caught_by=user.telegram_id, caught_at=datetime.utcnow())
        .returning(ActiveSpawn.id)
    )
    if claim.scalar_one_or_none() is None:
        await message.answer(" Too slow! Someone else caught it first.")
        return

    # Successful catch!
    import random
    import uuid

    # Generate IVs (use forced values from admin spawn if present, else random)
    ivs = {
        "hp": spawn.force_iv_hp if spawn.force_iv_hp is not None else random.randint(0, MAX_IV),
        "attack": spawn.force_iv_attack if spawn.force_iv_attack is not None else random.randint(0, MAX_IV),
        "defense": spawn.force_iv_defense if spawn.force_iv_defense is not None else random.randint(0, MAX_IV),
        "sp_attack": spawn.force_iv_sp_attack if spawn.force_iv_sp_attack is not None else random.randint(0, MAX_IV),
        "sp_defense": spawn.force_iv_sp_defense if spawn.force_iv_sp_defense is not None else random.randint(0, MAX_IV),
        "speed": spawn.force_iv_speed if spawn.force_iv_speed is not None else random.randint(0, MAX_IV),
    }

    # Determine nature
    nature = random_nature()

    # Pick ability
    abilities = spawn.species.abilities or ["unknown"]
    ability = random.choice(abilities)

    # Determine gender
    gender = determine_gender(spawn.species)

    # Determine level (use forced level from admin spawn if present)
    catch_level = spawn.force_level if spawn.force_level is not None else random.randint(CATCH_LEVEL_MIN, CATCH_LEVEL_MAX)

    # Shiny hunt payoff.  The spawn's shiny flag was rolled at base odds before
    # any catcher was known, so an active hunt on this species gets a second
    # conditional roll here that lifts the catcher's end-to-end odds to exactly
    # what /hunt advertises.  Nobody's odds get worse, and only the catcher's
    # own chain and charm count.
    is_shiny = spawn.is_shiny
    if not is_shiny and user.shiny_hunt_species_id == spawn.species_id:
        has_charm = await has_shiny_charm(session, user.telegram_id)
        bonus = shiny.bonus_probability(user.shiny_hunt_chain, has_charm)
        if bonus > 0 and random.random() < bonus:
            is_shiny = True

    # Create the Pokemon
    new_pokemon = Pokemon(
        id=uuid.uuid4(),
        owner_id=user.telegram_id,
        species_id=spawn.species_id,
        level=catch_level,
        iv_hp=ivs["hp"],
        iv_attack=ivs["attack"],
        iv_defense=ivs["defense"],
        iv_sp_attack=ivs["sp_attack"],
        iv_sp_defense=ivs["sp_defense"],
        iv_speed=ivs["speed"],
        nature=nature,
        ability=ability,
        is_shiny=is_shiny,
        gender=gender,
        original_trainer_id=user.telegram_id,
        caught_in_group_id=chat_id,
        form=regional.get_form(spawn.species_id).region
        if regional.is_regional(spawn.species_id)
        else None,
    )

    # Spawn was already marked caught by the atomic claim above.

    # Rewards — only from new dex entries, milestones, and quests
    reward = 0

    # Update pokedex
    is_new_pokedex_entry = False
    pokedex_bonus = 0
    pokedex_result = await session.execute(
        select(PokedexEntry)
        .where(PokedexEntry.user_id == user.telegram_id)
        .where(PokedexEntry.species_id == spawn.species_id)
    )
    pokedex_entry = pokedex_result.scalar_one_or_none()

    if pokedex_entry:
        pokedex_entry.caught = True
        pokedex_entry.times_caught += 1
        if is_shiny:
            pokedex_entry.caught_shiny = True

        # Milestone bonuses
        catches = pokedex_entry.times_caught
        if catches == 10:
            pokedex_bonus = 350
        elif catches == 100:
            pokedex_bonus = 3500
        elif catches == 1000:
            pokedex_bonus = 35000
    else:
        is_new_pokedex_entry = True
        pokedex_bonus = 35  # First catch bonus
        pokedex_entry = PokedexEntry(
            user_id=user.telegram_id,
            species_id=spawn.species_id,
            seen=True,
            caught=True,
            caught_shiny=is_shiny,
            times_caught=1,
            first_caught_at=datetime.utcnow(),
        )
        session.add(pokedex_entry)

    # Add pokedex bonus to reward
    reward += pokedex_bonus
    user.balance += pokedex_bonus

    session.add(new_pokemon)

    # Assign initial moves based on species learnset
    new_pokemon.species = spawn.species

    from telemon.core.moves import assign_starter_moves
    await assign_starter_moves(session, new_pokemon)

    await session.commit()

    iv_total = sum(ivs.values())
    iv_percent = iv_percentage(iv_total)

    # Initialize optional message fragments — these are set conditionally
    # below and referenced unconditionally in the response builder.
    chain_msg = None
    quest_msg = None
    xp_msg = None
    ach_notifications = None

    try:
        # Update quest progress
        from telemon.core.quests import update_quest_progress

        # Generic catch quest
        completed = await update_quest_progress(session, user.telegram_id, "catch")
        # Type-specific catch quest
        for ptype in spawn.species.types:
            completed += await update_quest_progress(
                session, user.telegram_id, "catch_type", params={"type": ptype.lower()}
            )
        # Shiny catch quest
        if is_shiny:
            completed += await update_quest_progress(session, user.telegram_id, "catch_shiny")
        if completed:
            await session.commit()
            quest_msg = "\n📋 Quest progress updated!"
            for q in completed:
                quest_msg += f"\n  🎉 Quest complete: {q.description} (+{q.reward_coins:,} {CURRENCY_SHORT})"

        # Update group stats
        group_result = await session.execute(
            select(Group).where(Group.chat_id == chat_id)
        )
        group = group_result.scalar_one_or_none()
        if group:
            group.total_catches += 1

        # Update shiny hunt chain
        if user.shiny_hunt_species_id:
            if user.shiny_hunt_species_id == spawn.species_id:
                # Correct species - increment chain
                user.shiny_hunt_chain += 1
                chain_msg = f"\n🔗 Chain: {user.shiny_hunt_chain}"
                if is_shiny:
                    chain_msg += " ✨ SHINY FOUND!"
            else:
                # Wrong species - break chain
                old_chain = user.shiny_hunt_chain
                if old_chain > 0:
                    user.shiny_hunt_chain = 0
                    chain_msg = f"\n⛓️‍💥 Chain broken! (was {old_chain})"

        # Friendship gain: selected Pokemon gets +1 per catch (Soothe Bell: +2)
        # Also award XP from catching
        if user.selected_pokemon_id:
            sel_result = await session.execute(
                select(Pokemon)
                .where(Pokemon.id == user.selected_pokemon_id)
                .where(Pokemon.owner_id == user.telegram_id)
            )
            sel_poke = sel_result.scalar_one_or_none()
            if sel_poke and sel_poke.friendship < MAX_FRIENDSHIP:
                gain = 1
                if sel_poke.held_item and sel_poke.held_item.lower() == "soothe bell":
                    gain = 2
                sel_poke.friendship = min(MAX_FRIENDSHIP, sel_poke.friendship + gain)

            # XP from catching
            if sel_poke and sel_poke.level < MAX_LEVEL:
                from telemon.core.leveling import calculate_catch_xp, add_xp_to_pokemon, format_xp_message, apply_xp_boost

                catch_xp = calculate_catch_xp(new_pokemon.level, spawn.species.catch_rate)
                catch_xp = apply_xp_boost(catch_xp, user.xp_boost_until)
                xp_added, levels_gained, learned_moves = await add_xp_to_pokemon(
                    session, str(sel_poke.id), catch_xp
                )
                if xp_added > 0:
                    xp_msg = "\n" + format_xp_message(esc(sel_poke.display_name), xp_added, levels_gained, learned_moves)

        # Achievement checks
        from telemon.core.achievements import check_achievements, format_achievement_notification

        ach_events = ["catch"]
        if is_shiny:
            ach_events.append("catch_shiny")
        if iv_percent >= 100.0:
            ach_events.append("catch_perfect")
        if spawn.species.is_legendary:
            ach_events.append("catch_legendary")
        if spawn.species.is_mythical:
            ach_events.append("catch_mythical")
        if is_new_pokedex_entry:
            ach_events.append("pokedex_update")

        all_new_achievements = []
        for ev in ach_events:
            all_new_achievements.extend(await check_achievements(session, user.telegram_id, ev))
        if all_new_achievements:
            await session.commit()
            ach_notifications = format_achievement_notification(all_new_achievements)

        # Team XP hook
        if user.team_id:
            from telemon.core.teams import add_team_xp
            await add_team_xp(session, user.team_id, "catch")

    except Exception as e:
        logger.error("Error in post-catch processing", error=str(e), exc_info=True)
        # We continue to send the success message even if secondary updates failed

    # Build response message
    shiny_text = " ✨ " if is_shiny else ""

    # IV quality rating
    if iv_percent >= 90:
        iv_rating = "⭐ Amazing!"
    elif iv_percent >= 75:
        iv_rating = "Great"
    elif iv_percent >= 50:
        iv_rating = "Good"
    elif iv_percent >= 25:
        iv_rating = "Average"
    else:
        iv_rating = "Poor"

    sprite = poke_emoji(spawn.species.national_dex)
    msg_lines = [
        f"<b>Congratulations {esc(user.display_name)}!</b> You caught a level {new_pokemon.level}{shiny_text}{sprite}<b>{spawn.species.name}</b>! (IV: {iv_percent:.2f}%)",
        # f"IVs: {iv_percent}% ({iv_rating})",
    ]

    # Reward messages — only for new dex entries, milestones, quests
    if is_new_pokedex_entry:
        msg_lines.append(f"\n\n📖 New Pokédex entry! (+{pokedex_bonus} {CURRENCY_NAME})")
    elif pokedex_bonus > 0:
        catches = pokedex_entry.times_caught
        msg_lines.append(f"\n\n🏆 {catches}x catch milestone! (+{pokedex_bonus} {CURRENCY_NAME})")

    # Shiny chain message
    if chain_msg:
        msg_lines.append(f"\n{chain_msg}")

    # XP gain message
    if xp_msg:
        msg_lines.append(f"\n{xp_msg}")

    # Quest progress
    if quest_msg:
        msg_lines.append(f"\n{quest_msg}")

    # Achievements
    if ach_notifications:
        msg_lines.append(f"\n{ach_notifications}")

    await message.answer("\n".join(msg_lines))


@router.message(Command("hint"))
async def cmd_hint(message: Message, session: AsyncSession, user: User) -> None:
    """Handle /hint command with cooldown."""
    chat_id = message.chat.id
    user_id = user.telegram_id

    # Check cooldown
    cooldown_key = (chat_id, user_id)
    now = datetime.utcnow()
    
    if cooldown_key in _hint_cooldowns:
        last_hint = _hint_cooldowns[cooldown_key]
        time_since = (now - last_hint).total_seconds()
        if time_since < HINT_COOLDOWN_SECONDS:
            remaining = int(HINT_COOLDOWN_SECONDS - time_since)
            await message.answer(f"Please wait {remaining}s before using /hint again!")
            return

    # Get active spawn
    result = await session.execute(
        select(ActiveSpawn)
        .where(ActiveSpawn.chat_id == chat_id)
        .where(ActiveSpawn.caught_by.is_(None))
        .where(ActiveSpawn.expires_at > datetime.utcnow())
        .order_by(ActiveSpawn.spawned_at.desc())
        .limit(1)
    )
    spawn = result.scalar_one_or_none()

    if not spawn:
        await message.answer("There's no wild Pokemon here right now!")
        return

    # Mark Pokemon as seen in user's pokedex
    pokedex_result = await session.execute(
        select(PokedexEntry)
        .where(PokedexEntry.user_id == user_id)
        .where(PokedexEntry.species_id == spawn.species_id)
    )
    pokedex_entry = pokedex_result.scalar_one_or_none()

    if not pokedex_entry:
        pokedex_entry = PokedexEntry(
            user_id=user_id,
            species_id=spawn.species_id,
            seen=True,
            caught=False,
            caught_shiny=False,
            times_caught=0,
        )
        session.add(pokedex_entry)
    elif not pokedex_entry.seen:
        pokedex_entry.seen = True

    # Update cooldown
    _hint_cooldowns[cooldown_key] = now

    # Prune expired cooldown entries to prevent memory leaks
    if len(_hint_cooldowns) > _HINT_COOLDOWN_MAX_SIZE:
        cutoff = now - timedelta(seconds=HINT_COOLDOWN_SECONDS)
        expired = [k for k, ts in _hint_cooldowns.items() if ts < cutoff]
        for k in expired:
            del _hint_cooldowns[k]

    # Generate hint.  For a regional form the region is given away for free and
    # only the species part is masked -- masking the whole "Alolan Vulpix" would
    # make the letter count and first letter useless to the player.
    form = regional.get_form(spawn.species_id)
    if form:
        short = spawn.species.name.split(" ", 1)[1]
        hint = f"{spawn.species.name.split(' ', 1)[0]} {generate_hint(short, spawn.hints_used)}"
    else:
        hint = generate_hint(spawn.species.name, spawn.hints_used)
    spawn.hints_used += 1
    await session.commit()

    await message.answer(f"Hint: <code>{hint}</code>")
