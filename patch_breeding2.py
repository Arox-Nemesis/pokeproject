import re

with open("src/telemon/bot/handlers/breeding.py", "r") as f:
    content = f.read()

# Add BREED_COOLDOWN_SECONDS import
if "BREED_COOLDOWN_SECONDS" not in content:
    content = content.replace(
        "from telemon.core.constants import iv_percentage",
        "from telemon.core.constants import BREED_COOLDOWN_SECONDS, iv_percentage"
    )

# Add imports for datetime, timedelta
if "from datetime import datetime, timedelta" not in content:
    content = content.replace(
        "from aiogram import Router",
        "from datetime import datetime, timedelta\n\nfrom aiogram import Router"
    )

# Add cooldown dict
cooldown_dict_code = """

# ---------------------------------------------------------------------------
# Cooldown Tracking
# ---------------------------------------------------------------------------
_breed_cooldowns: dict[int, datetime] = {}
_BREED_COOLDOWN_MAX_SIZE = 1000

"""

if "_breed_cooldowns" not in content:
    content = content.replace(
        "logger = get_logger(__name__)\n",
        "logger = get_logger(__name__)\n" + cooldown_dict_code
    )

old_cmd_breed = """@router.message(Command("breed"))
async def cmd_breed(message: Message, session: AsyncSession) -> None:
    \"\"\"Attempt to breed the two Pokemon in daycare.\"\"\"
    if not message.from_user:
        return

    user_id = message.from_user.id
    slots = await get_daycare_slots(session, user_id)

    if len(slots) < 2:
        await message.answer(
            "You need 2 Pokemon in daycare to breed.\\n"
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

    species_name = egg.species.name if egg.species else f"#{egg.species_id}"
    shiny_text = " ✨" if egg.is_shiny else ""
    iv_total = (
        egg.iv_hp + egg.iv_attack + egg.iv_defense
        + egg.iv_sp_attack + egg.iv_sp_defense + egg.iv_speed
    )
    iv_pct = iv_percentage(iv_total)

    await message.answer(
        f"🥚 <b>An egg appeared!</b>{shiny_text}\\n"
        f"Species: <b>{species_name}</b> | IV: {iv_pct}% | Steps: {egg.steps_total}\\n"
        f"<i>Send messages in groups to add steps! Use /eggs to check, /hatch when ready.</i>"
    )"""

new_cmd_breed = """@router.message(Command("breed"))
async def cmd_breed(message: Message, session: AsyncSession) -> None:
    \"\"\"Attempt to breed the two Pokemon in daycare.\"\"\"
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
            "You need 2 Pokemon in daycare to breed.\\n"
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
        f"🥚 <b>An egg appeared!</b>{shiny_text}\\n"
        f"Species: <b>{species_name}</b> | IV: {iv_pct}% | Steps: {egg.steps_total}\\n"
        f"<i>Send messages in groups to add steps! Use /eggs to check, /hatch when ready.</i>"
    )"""

content = content.replace(old_cmd_breed, new_cmd_breed)

with open("src/telemon/bot/handlers/breeding.py", "w") as f:
    f.write(content)
