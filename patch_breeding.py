import re
import sys

with open("src/telemon/bot/handlers/breeding.py", "r") as f:
    content = f.read()

# Add BREED_COOLDOWN_SECONDS import
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
elif "from datetime import " in content:
    content = content.replace(
        "from datetime import ",
        "from datetime import datetime, timedelta, "
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

# Add check to cmd_breed
breed_check_code = """
    user_id = message.from_user.id
    now = datetime.utcnow()

    # Cooldown check
    if user_id in _breed_cooldowns:
        elapsed = (now - _breed_cooldowns[user_id]).total_seconds()
        if elapsed < BREED_COOLDOWN_SECONDS:
            remaining = int(BREED_COOLDOWN_SECONDS - elapsed)
            mins = remaining // 60
            secs = remaining % 60
            await message.answer(f"⏳ Breeding cooldown! Your Pokemon need to rest for {mins}m {secs}s.")
            return

    # Prune expired cooldowns to prevent memory leak
    if len(_breed_cooldowns) > _BREED_COOLDOWN_MAX_SIZE:
        cutoff = now - timedelta(seconds=BREED_COOLDOWN_SECONDS)
        expired = [uid for uid, ts in _breed_cooldowns.items() if ts < cutoff]
        for uid in expired:
            del _breed_cooldowns[uid]

    slots = await get_daycare_slots(session, user_id)
"""

content = re.sub(
    r"    user_id = message\.from_user\.id\n    slots = await get_daycare_slots\(session, user_id\)",
    breed_check_code,
    content
)

# Add success to cmd_breed
success_code = """
    # Set cooldown
    _breed_cooldowns[user_id] = datetime.utcnow()

    species_name = egg.species.name if egg.species else f"#{egg.species_id}"
"""

content = re.sub(
    r"    species_name = egg\.species\.name if egg\.species else f\"#\{egg\.species_id\}\"",
    success_code,
    content
)

with open("src/telemon/bot/handlers/breeding.py", "w") as f:
    f.write(content)
