"""Admin and group settings handlers."""

import random
from datetime import datetime

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.config import BOT_OWNER_ID
from telemon.core.constants import VALID_TYPES, RARITY_KEYWORDS, MAX_GENERATION
from telemon.core.spawning import create_spawn, get_random_species
from telemon.database.models import ActiveSpawn, BotConfig, Group, Pokemon, PokemonSpecies, SpawnAdmin, User
from telemon.database.models.spawn_admin import SPAWN_PERMISSIONS
from telemon.logging import get_logger
from telemon.core.text import esc

router = Router(name="admin")
logger = get_logger(__name__)


# ------------------------------------------------------------------ #
# Persistent runtime configuration
# ------------------------------------------------------------------ #

_runtime_overrides: dict[str, int] = {}

# Allowed config keys: (min, max, description)
_CONFIG_KEYS: dict[str, tuple[int, int, str]] = {
    "incense_count": (1, 500, "Number of spawns per incense"),
    "flee_enabled": (0, 1, "Whether wild spawns flee after timeout (0=off, 1=on)"),
    "spawn_timeout": (30, 3600, "Seconds before a wild spawn flees"),
    "timed_spawn_min": (1, 120, "Minimum minutes between inactivity spawns"),
    "timed_spawn_max": (1, 120, "Maximum minutes between inactivity spawns"),
}

# Default values used when no override is set (matches settings.py defaults)
_CONFIG_DEFAULTS: dict[str, int] = {
    "flee_enabled": 1,
    "timed_spawn_min": 10,
    "timed_spawn_max": 20,
}


def get_runtime_config(key: str, default: int) -> int:
    """Get a runtime-configurable value, falling back to default."""
    return _runtime_overrides.get(key, default)


async def load_runtime_config(session: AsyncSession) -> None:
    """Load all persisted config from DB into the in-memory cache.

    Call once at bot startup.
    """
    result = await session.execute(select(BotConfig))
    rows = result.scalars().all()
    for row in rows:
        try:
            _runtime_overrides[row.key] = int(row.value)
        except ValueError:
            logger.warning("Ignoring non-integer config", key=row.key, value=row.value)
    if rows:
        logger.info("Loaded persistent runtime config", count=len(rows), keys=list(_runtime_overrides.keys()))


async def _persist_config(session: AsyncSession, key: str, value: int) -> None:
    """Write a single config key to the database and update the cache."""
    _runtime_overrides[key] = value

    result = await session.execute(select(BotConfig).where(BotConfig.key == key))
    existing = result.scalar_one_or_none()
    if existing:
        existing.value = str(value)
    else:
        session.add(BotConfig(key=key, value=str(value)))
    await session.commit()


# ------------------------------------------------------------------ #
# Permission helpers
# ------------------------------------------------------------------ #

async def get_spawn_admin(session: AsyncSession, user_id: int) -> SpawnAdmin | None:
    """Get SpawnAdmin record for a user (None if not a spawner)."""
    result = await session.execute(
        select(SpawnAdmin).where(SpawnAdmin.user_id == user_id)
    )
    return result.scalar_one_or_none()


def _is_owner(user_id: int) -> bool:
    return user_id == BOT_OWNER_ID


async def is_spawn_admin(session: AsyncSession, user_id: int) -> bool:
    """Check if user is allowed to use /spawn command."""
    if _is_owner(user_id):
        return True
    return await get_spawn_admin(session, user_id) is not None


def _check_perm(user_id: int, admin: SpawnAdmin | None, perm: str) -> bool:
    """Check if a user has a specific spawn permission."""
    if _is_owner(user_id):
        return True  # Owner has all permissions
    if admin is None:
        return False
    return admin.has_perm(perm)


# ------------------------------------------------------------------ #
# Spawn argument parser
# ------------------------------------------------------------------ #

# Mapping of short stat token prefixes to force_stats dict keys
_STAT_TOKEN_MAP: dict[str, str] = {
    "hp:": "iv_hp",
    "atk:": "iv_attack",
    "def:": "iv_defense",
    "spa:": "iv_sp_attack",
    "spd:": "iv_sp_defense",
    "spe:": "iv_speed",
}

# All six IV keys in the stats dict (stable order for distribution)
_IV_KEYS = ("iv_hp", "iv_attack", "iv_defense", "iv_sp_attack", "iv_sp_defense", "iv_speed")


def _parse_spawn_args(text: str) -> dict:
    """Parse /spawn arguments into a structured dict.

    Returns:
        {
            "name": str | None,
            "gen": int | None,
            "type": str | None,
            "rarity": str | None,
            "shiny": bool,
            "stats": {
                "level": int | None,
                "iv_percent": int | None,   # target IV% (0-100)
                "iv_hp": int | None,        # individual forced IVs (0-31)
                "iv_attack": int | None,
                "iv_defense": int | None,
                "iv_sp_attack": int | None,
                "iv_sp_defense": int | None,
                "iv_speed": int | None,
            },
            "perms_needed": set[str],
        }
    """
    result: dict = {
        "name": None,
        "gen": None,
        "type": None,
        "rarity": None,
        "shiny": False,
        "stats": {
            "level": None,
            "iv_percent": None,
            "iv_hp": None,
            "iv_attack": None,
            "iv_defense": None,
            "iv_sp_attack": None,
            "iv_sp_defense": None,
            "iv_speed": None,
        },
        "perms_needed": set(),
    }

    # Strip /spawn prefix
    raw = text.strip()
    if raw.startswith("/spawn"):
        raw = raw[6:].strip()
    # Also strip @bot suffix from command
    if raw.startswith("@"):
        at_end = raw.find(" ")
        raw = raw[at_end:].strip() if at_end != -1 else ""

    if not raw:
        return result  # Plain /spawn — random, no perms needed

    tokens = raw.split()
    name_parts: list[str] = []

    for token in tokens:
        lower = token.lower().rstrip(",")

        # --shiny flag
        if lower in ("--shiny", "-s"):
            result["shiny"] = True
            result["perms_needed"].add("shiny")
            continue

        # gen:N filter
        if lower.startswith("gen:"):
            try:
                gen = int(lower.split(":", 1)[1])
                if 1 <= gen <= MAX_GENERATION:
                    result["gen"] = gen
                    result["perms_needed"].add("gen")
            except ValueError:
                pass
            continue

        # type:X filter
        if lower.startswith("type:"):
            ptype = lower.split(":", 1)[1]
            if ptype in VALID_TYPES:
                result["type"] = ptype
                result["perms_needed"].add("type")
            continue

        # level:N — force catch level (perm: stats)
        if lower.startswith("level:"):
            try:
                lvl = int(lower.split(":", 1)[1])
                if 1 <= lvl <= 100:
                    result["stats"]["level"] = lvl
                    result["perms_needed"].add("stats")
            except ValueError:
                pass
            continue

        # iv:N — target N% IV total (perm: stats)
        if lower.startswith("iv:"):
            try:
                pct = int(lower.split(":", 1)[1])
                if 0 <= pct <= 100:
                    result["stats"]["iv_percent"] = pct
                    result["perms_needed"].add("stats")
            except ValueError:
                pass
            continue

        # Individual stat tokens: hp:N, atk:N, def:N, etc. (perm: stats)
        stat_matched = False
        for prefix, stat_key in _STAT_TOKEN_MAP.items():
            if lower.startswith(prefix):
                try:
                    val = int(lower.split(":", 1)[1])
                    if 0 <= val <= 31:
                        result["stats"][stat_key] = val
                        result["perms_needed"].add("stats")
                except ValueError:
                    pass
                stat_matched = True
                break
        if stat_matched:
            continue

        # Rarity keywords
        if lower in RARITY_KEYWORDS:
            result["rarity"] = lower
            result["perms_needed"].add("rarity")
            continue

        # Everything else is part of the Pokemon name
        name_parts.append(token)

    if name_parts:
        result["name"] = " ".join(name_parts)
        result["perms_needed"].add("name")

    return result


def _distribute_iv_total(total: int, n: int) -> list[int]:
    """Distribute *total* IV points across *n* stats (each 0-31).

    Returns a list of *n* ints summing to *total*, each in [0, 31].
    The values are shuffled for randomness.
    """
    stats: list[int] = []
    remaining = total
    for i in range(n - 1):
        slots_left = n - i
        lo = max(0, remaining - (slots_left - 1) * 31)
        hi = min(31, remaining)
        val = random.randint(lo, hi)
        stats.append(val)
        remaining -= val
    stats.append(remaining)  # last slot gets the remainder
    random.shuffle(stats)
    return stats


def _resolve_forced_stats(raw_stats: dict) -> tuple[dict | None, str | None]:
    """Resolve parsed stats into final force_stats dict for create_spawn.

    Handles five scenarios:
      1. Only iv:N%        → distribute across all 6
      2. Only individual    → use them, rest random
      3. iv:N% + some       → distribute remainder across unfixed
      4. iv:N% + all 6      → validate sum matches target
      5. All 6 no iv:       → use all, IV% is whatever it sums to

    Returns (force_stats, error_message).
      - force_stats is None when no stats were requested.
      - error_message is None on success.
    """
    iv_percent: int | None = raw_stats.get("iv_percent")
    level: int | None = raw_stats.get("level")

    # Gather individual IV overrides
    fixed: dict[str, int] = {}  # key -> value for specified IVs
    unfixed: list[str] = []     # keys for unspecified IVs
    for key in _IV_KEYS:
        val = raw_stats.get(key)
        if val is not None:
            fixed[key] = val
        else:
            unfixed.append(key)

    has_iv_target = iv_percent is not None
    has_individual = len(fixed) > 0
    has_level = level is not None

    # Nothing requested
    if not has_iv_target and not has_individual and not has_level:
        return None, None

    result: dict = {}
    if has_level:
        result["level"] = level

    # --- Scenario 2 / 5: no iv target, just individual stats ---
    if not has_iv_target:
        # Use specified values; unspecified stay None (random at catch)
        for key, val in fixed.items():
            result[key] = val
        return result, None

    # --- We have an iv:N% target ---
    max_total = 6 * 31  # 186
    target_total = round(iv_percent / 100 * max_total)
    target_total = max(0, min(max_total, target_total))  # clamp

    fixed_sum = sum(fixed.values())

    # Scenario 4: all 6 specified + iv target
    if len(fixed) == 6:
        if fixed_sum != target_total:
            actual_pct = round(fixed_sum / max_total * 100, 1)
            return None, (
                f"Your stats total <b>{actual_pct}%</b> IV "
                f"but you specified <b>iv:{iv_percent}</b>.\n"
                f"Remove one stat to allow auto-adjustment, "
                f"or change the iv: value."
            )
        # Sum matches — use all as-is
        for key, val in fixed.items():
            result[key] = val
        return result, None

    # Scenario 1 or 3: distribute remaining budget across unfixed stats
    remaining_budget = target_total - fixed_sum
    n_unfixed = len(unfixed)

    if remaining_budget < 0:
        return None, (
            f"The stats you specified already total <b>{fixed_sum}</b> points, "
            f"which exceeds the iv:{iv_percent} target of <b>{target_total}</b>.\n"
            f"Lower some stat values or increase iv:N."
        )
    if remaining_budget > n_unfixed * 31:
        min_needed = round((fixed_sum + n_unfixed * 31) / max_total * 100)
        return None, (
            f"Can't reach iv:{iv_percent} — the {n_unfixed} remaining stats "
            f"can contribute at most <b>{n_unfixed * 31}</b> points, "
            f"but <b>{remaining_budget}</b> are needed.\n"
            f"Increase some stat values or lower iv: to at most <b>{min_needed}</b>."
        )

    # Distribute remaining budget
    distributed = _distribute_iv_total(remaining_budget, n_unfixed)
    for key, val in fixed.items():
        result[key] = val
    for key, val in zip(unfixed, distributed):
        result[key] = val

    return result, None


# ------------------------------------------------------------------ #
# Species resolver
# ------------------------------------------------------------------ #

async def _resolve_species(
    session: AsyncSession, args: dict
) -> tuple[PokemonSpecies | None, str | None]:
    """Resolve a PokemonSpecies based on parsed spawn args.

    Returns (species, error_message).
    """
    query = select(PokemonSpecies)

    # By name — exact match
    if args["name"]:
        name_lower = args["name"].lower().replace(" ", "-")
        result = await session.execute(
            query.where(PokemonSpecies.name_lower == name_lower)
        )
        species = result.scalar_one_or_none()
        if not species:
            # Try partial match
            result = await session.execute(
                query.where(PokemonSpecies.name_lower.ilike(f"%{name_lower}%"))
            )
            matches = result.scalars().all()
            if len(matches) == 1:
                species = matches[0]
            elif len(matches) > 1:
                names = ", ".join(m.name for m in matches[:10])
                return None, f"Multiple matches: {names}. Be more specific."
            else:
                # Try dex number
                try:
                    dex = int(args["name"])
                    result = await session.execute(
                        query.where(PokemonSpecies.national_dex == dex)
                    )
                    species = result.scalar_one_or_none()
                except ValueError:
                    pass
                if not species:
                    return None, f"Pokemon '{esc(args['name'])}' not found."

        # Block Mega forms — they must only be acquired via /mega
        if species and species.name_lower.startswith("mega "):
            return None, (
                f"<b>{species.name}</b> can't be spawned directly.\n"
                "Mega Pokémon can only be acquired through /mega evolution."
            )
        return species, None

    # Build filter query for random selection — exclude forms (Megas, regionals)
    filters = [PokemonSpecies.national_dex < 10000]

    if args["gen"]:
        filters.append(PokemonSpecies.generation == args["gen"])

    if args["type"]:
        ptype = args["type"]
        filters.append(
            (PokemonSpecies.type1 == ptype) | (PokemonSpecies.type2 == ptype)
        )

    if args["rarity"]:
        # Reuse the spawn engine's tier definition so /spawn and wild spawns
        # can never disagree about what counts as e.g. "rare".
        from telemon.core.spawning.engine import _rarity_condition

        filters.append(_rarity_condition(args["rarity"]))

    if filters:
        for f in filters:
            query = query.where(f)
        result = await session.execute(query)
        candidates = result.scalars().all()
        if not candidates:
            return None, "No Pokemon match those filters."
        return random.choice(candidates), None

    # No filters — use weighted random
    return await get_random_species(session), None


# ------------------------------------------------------------------ #
# /spawn command
# ------------------------------------------------------------------ #

@router.message(Command("spawn"))
async def cmd_spawn(message: Message, session: AsyncSession, bot: Bot) -> None:
    """Handle /spawn command to force a Pokemon spawn.

    Syntax:
        /spawn                        -- random (any spawner)
        /spawn Rayquaza               -- by name (perm: name)
        /spawn gen:3                  -- by generation (perm: gen)
        /spawn type:fire              -- by type (perm: type)
        /spawn legendary              -- by rarity (perm: rarity)
        /spawn --shiny                -- force shiny (perm: shiny)
        /spawn Rayquaza --shiny       -- combinable
        /spawn gen:5 type:dragon --shiny
    """
    if message.chat.type == "private":
        await message.answer("This command only works in groups!")
        return
    if not message.from_user:
        return

    user_id = message.from_user.id

    # Check basic spawn admin access
    has_access = await is_spawn_admin(session, user_id)
    if not has_access:
        await message.answer("You don't have permission to use /spawn!")
        return

    # Parse arguments
    args = _parse_spawn_args(message.text or "")

    # Check granular permissions
    admin = await get_spawn_admin(session, user_id)
    missing_perms: list[str] = []
    for perm in args["perms_needed"]:
        if not _check_perm(user_id, admin, perm):
            missing_perms.append(perm)

    if missing_perms:
        await message.answer(
            f"You don't have permission for: <b>{', '.join(missing_perms)}</b>\n"
            f"Ask the bot owner to grant them via /grant."
        )
        return

    chat_id = message.chat.id

    # Get or create group
    result = await session.execute(select(Group).where(Group.chat_id == chat_id))
    group = result.scalar_one_or_none()

    if not group:
        group = Group(
            chat_id=chat_id,
            title=message.chat.title,
            bot_joined_at=datetime.utcnow(),
        )
        session.add(group)
        await session.flush()

    # Check for existing active spawn
    result = await session.execute(
        select(ActiveSpawn)
        .where(ActiveSpawn.chat_id == chat_id)
        .where(ActiveSpawn.caught_by.is_(None))
        .where(ActiveSpawn.expires_at > datetime.utcnow())
    )
    existing = result.scalar_one_or_none()

    if existing:
        await message.answer(
            "There's already an active spawn in this group!\n"
            "Use /catch [name] to catch it first."
        )
        return

    # Resolve species based on args
    species, error = await _resolve_species(session, args)
    if error:
        await message.answer(f"x {error}")
        return
    if not species:
        await message.answer("No Pokemon species found in database!")
        return

    # Resolve stats (distribution + conflict detection)
    force_stats, stats_error = _resolve_forced_stats(args["stats"])
    if stats_error:
        await message.answer(f"⚠️ {stats_error}")
        return

    # Create spawn
    spawn = await create_spawn(
        session=session,
        chat_id=chat_id,
        message_id=0,
        species=species,
        force_shiny=args["shiny"],
        force_stats=force_stats,
    )

    if not spawn:
        await message.answer("Failed to create spawn!")
        return

    # Send spawn message
    from telemon.bot.handlers.spawn import send_spawn_message

    try:
        msg_id = await send_spawn_message(bot, chat_id, spawn)
        if msg_id:
            spawn.message_id = msg_id
            await session.commit()

            # Build log details
            details: list[str] = [species.name]
            if args["gen"]:
                details.append(f"gen:{esc(args['gen'])}")
            if args["type"]:
                details.append(f"type:{esc(args['type'])}")
            if args["rarity"]:
                details.append(args["rarity"])
            if args["shiny"]:
                details.append("shiny")
            if force_stats:
                details.append(f"stats:{force_stats}")

            logger.info(
                "Admin force spawned Pokemon",
                chat_id=chat_id,
                species=species.name,
                is_shiny=spawn.is_shiny,
                admin_id=user_id,
                filters=" ".join(details),
            )
        else:
            await message.answer("Failed to send spawn message!")
    except Exception as e:
        logger.error("Failed to send spawn message", error=str(e), chat_id=chat_id)
        await message.answer(f"Failed to send spawn message: {e}")


# ------------------------------------------------------------------ #
# /addspawner  /removespawner
# ------------------------------------------------------------------ #

@router.message(Command("addspawner"))
async def cmd_add_spawner(message: Message, session: AsyncSession) -> None:
    """Add a user to spawn admins list. Bot owner only."""
    if not message.from_user:
        return
    if message.from_user.id != BOT_OWNER_ID:
        await message.answer("Only the bot owner can use this command!")
        return

    target_user_id, target_username = _extract_target(message)

    if not target_user_id:
        await message.answer(
            "<b>Usage:</b> Reply to a message from the user you want to add.\n\n"
            "Example: Reply to someone's message and type /addspawner"
        )
        return

    # Check if already exists
    existing = await get_spawn_admin(session, target_user_id)
    if existing:
        await message.answer(f"User {esc(target_username)} is already a spawn admin!")
        return

    spawn_admin = SpawnAdmin(
        user_id=target_user_id,
        added_by=message.from_user.id,
        permissions=[],  # Default: random only
    )
    session.add(spawn_admin)
    await session.commit()

    await message.answer(
        f"Added <b>{esc(target_username)}</b> as a spawn admin!\n"
        f"Permissions: <b>random only</b>\n\n"
        f"Use <code>/grant {target_user_id} [perm]</code> to add permissions.\n"
        f"Available: name, gen, type, rarity, shiny, all"
    )
    logger.info("Added spawn admin", user_id=target_user_id, added_by=message.from_user.id)


@router.message(Command("removespawner"))
async def cmd_remove_spawner(message: Message, session: AsyncSession) -> None:
    """Remove a user from spawn admins list. Bot owner only."""
    if not message.from_user:
        return
    if message.from_user.id != BOT_OWNER_ID:
        await message.answer("Only the bot owner can use this command!")
        return

    target_user_id, target_username = _extract_target(message)

    if not target_user_id:
        await message.answer(
            "<b>Usage:</b> Reply to a message from the user you want to remove.\n\n"
            "Example: Reply to someone's message and type /removespawner"
        )
        return

    existing = await get_spawn_admin(session, target_user_id)
    if not existing:
        await message.answer(f"User {esc(target_username)} is not a spawn admin!")
        return

    await session.execute(
        delete(SpawnAdmin).where(SpawnAdmin.user_id == target_user_id)
    )
    await session.commit()

    await message.answer(f"Removed {esc(target_username)} from spawn admins!")
    logger.info("Removed spawn admin", user_id=target_user_id, removed_by=message.from_user.id)


# ------------------------------------------------------------------ #
# /grant  /revoke  — manage spawner permissions
# ------------------------------------------------------------------ #

@router.message(Command("grant"))
async def cmd_grant(message: Message, session: AsyncSession) -> None:
    """Grant spawn permissions to a spawner. Bot owner only.

    Usage:
        /grant <user_id> <perm1> [perm2] ...
        /grant (reply) <perm1> [perm2] ...
    """
    if not message.from_user or message.from_user.id != BOT_OWNER_ID:
        await message.answer("Only the bot owner can use this command!")
        return

    parts = (message.text or "").split()
    # Remove /grant
    parts = parts[1:] if parts else []

    # Determine target and perms
    target_user_id = None
    perm_tokens: list[str] = []

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user_id = message.reply_to_message.from_user.id
        perm_tokens = parts  # All args are perms
    elif parts:
        # First arg might be user_id
        try:
            target_user_id = int(parts[0])
            perm_tokens = parts[1:]
        except ValueError:
            await message.answer(
                "<b>Usage:</b>\n"
                "<code>/grant [user_id] [perm1] [perm2] ...</code>\n"
                "Or reply to the user's message.\n\n"
                f"<b>Available perms:</b> {', '.join(sorted(SPAWN_PERMISSIONS))}"
            )
            return

    if not target_user_id or not perm_tokens:
        await message.answer(
            "<b>Usage:</b>\n"
            "<code>/grant [user_id] [perm1] [perm2] ...</code>\n"
            "Or reply to the user's message.\n\n"
            f"<b>Available perms:</b> {', '.join(sorted(SPAWN_PERMISSIONS))}"
        )
        return

    # Validate perms
    requested = {p.lower() for p in perm_tokens}
    invalid = requested - SPAWN_PERMISSIONS
    if invalid:
        await message.answer(
            f"Invalid permissions: {', '.join(invalid)}\n"
            f"Available: {', '.join(sorted(SPAWN_PERMISSIONS))}"
        )
        return

    admin = await get_spawn_admin(session, target_user_id)
    if not admin:
        await message.answer(
            f"User <code>{target_user_id}</code> is not a spawn admin.\n"
            "Add them first with /addspawner."
        )
        return

    # Merge permissions
    current = set(admin.permissions or [])
    current |= requested
    admin.permissions = sorted(current)
    await session.commit()

    await message.answer(
        f"Granted <b>{', '.join(sorted(requested))}</b> to user <code>{target_user_id}</code>.\n"
        f"Current perms: <b>{admin.perm_display()}</b>"
    )
    logger.info("Granted spawn perms", user_id=target_user_id, granted=sorted(requested))


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, session: AsyncSession) -> None:
    """Revoke spawn permissions from a spawner. Bot owner only.

    Usage:
        /revoke <user_id> <perm1> [perm2] ...
        /revoke (reply) <perm1> [perm2] ...
    """
    if not message.from_user or message.from_user.id != BOT_OWNER_ID:
        await message.answer("Only the bot owner can use this command!")
        return

    parts = (message.text or "").split()
    parts = parts[1:] if parts else []

    target_user_id = None
    perm_tokens: list[str] = []

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user_id = message.reply_to_message.from_user.id
        perm_tokens = parts
    elif parts:
        try:
            target_user_id = int(parts[0])
            perm_tokens = parts[1:]
        except ValueError:
            await message.answer(
                "<b>Usage:</b>\n"
                "<code>/revoke [user_id] [perm1] [perm2] ...</code>\n"
                "Or reply to the user's message."
            )
            return

    if not target_user_id or not perm_tokens:
        await message.answer(
            "<b>Usage:</b>\n"
            "<code>/revoke [user_id] [perm1] [perm2] ...</code>\n"
            "Or reply to the user's message."
        )
        return

    requested = {p.lower() for p in perm_tokens}

    admin = await get_spawn_admin(session, target_user_id)
    if not admin:
        await message.answer(f"User <code>{target_user_id}</code> is not a spawn admin.")
        return

    current = set(admin.permissions or [])
    removed = current & requested
    current -= requested
    admin.permissions = sorted(current) if current else []
    await session.commit()

    if removed:
        await message.answer(
            f"Revoked <b>{', '.join(sorted(removed))}</b> from user <code>{target_user_id}</code>.\n"
            f"Current perms: <b>{admin.perm_display()}</b>"
        )
    else:
        await message.answer(
            f"User <code>{target_user_id}</code> didn't have those permissions."
        )
    logger.info("Revoked spawn perms", user_id=target_user_id, revoked=sorted(removed))


# ------------------------------------------------------------------ #
# /spawners  — list all spawn admins with permissions
# ------------------------------------------------------------------ #

@router.message(Command("spawners"))
async def cmd_list_spawners(message: Message, session: AsyncSession) -> None:
    """List all spawn admins with their permissions. Bot owner only."""
    if not message.from_user:
        return
    if message.from_user.id != BOT_OWNER_ID:
        await message.answer("Only the bot owner can use this command!")
        return

    result = await session.execute(select(SpawnAdmin))
    spawn_admins = result.scalars().all()

    if not spawn_admins:
        await message.answer(
            "<b>Spawn Admins</b>\n\n"
            "No spawn admins added yet.\n\n"
            "Use /addspawner by replying to a user's message to add them."
        )
        return

    lines = ["<b>Spawn Admins</b>\n"]
    for i, admin in enumerate(spawn_admins, 1):
        added_at = admin.created_at.strftime("%Y-%m-%d") if admin.created_at else "?"
        perms = admin.perm_display()
        lines.append(
            f"{i}. <code>{admin.user_id}</code> -- <b>{perms}</b> (added: {added_at})"
        )

    lines.append(f"\nTotal: {len(spawn_admins)} spawn admin(s)")
    lines.append("Note: Bot owner always has full access.")
    lines.append(
        "\n<b>Manage:</b>\n"
        "<code>/grant [user_id] [perm]</code>\n"
        "<code>/revoke [user_id] [perm]</code>\n"
        f"Perms: {', '.join(sorted(SPAWN_PERMISSIONS))}"
    )

    await message.answer("\n".join(lines))


# ------------------------------------------------------------------ #
# /setconfig  — bot owner runtime config
# ------------------------------------------------------------------ #

@router.message(Command("setconfig"))
async def cmd_setconfig(message: Message, session: AsyncSession) -> None:
    """Set runtime configuration values. Bot owner only.

    Usage:
        /setconfig                      — show current values
        /setconfig incense_count 30     — set incense spawn count to 30
        /setconfig flee_enabled 0       — disable flee timer
        /setconfig spawn_timeout 600    — set flee timer to 10 minutes
    """
    if not message.from_user or message.from_user.id != BOT_OWNER_ID:
        await message.answer("Only the bot owner can use this command!")
        return

    parts = (message.text or "").split()
    parts = parts[1:]  # Remove /setconfig

    if not parts:
        from telemon.config import settings
        lines = ["<b>Runtime Config</b>\n"]
        for key, (lo, hi, desc) in _CONFIG_KEYS.items():
            current = _runtime_overrides.get(key)
            if key == "incense_count":
                default = settings.incense_spawn_count
            elif key == "spawn_timeout":
                default = settings.spawn_timeout_seconds
            else:
                default = _CONFIG_DEFAULTS.get(key, "?")
            if current is not None:
                lines.append(f"<b>{key}</b>: {current} (override, default: {default})")
            else:
                lines.append(f"<b>{key}</b>: {default} (default, range: {lo}-{hi})")
            lines.append(f"  <i>{desc}</i>")
        lines.append("\n<i>Usage: /setconfig [key] [value]</i>")
        await message.answer("\n".join(lines))
        return

    if len(parts) < 2:
        await message.answer(
            "Usage: <code>/setconfig [key] [value]</code>\n\n"
            f"Available keys: {', '.join(_CONFIG_KEYS.keys())}"
        )
        return

    key = parts[0].lower()
    if key not in _CONFIG_KEYS:
        await message.answer(
            f"Unknown config key: <b>{key}</b>\n"
            f"Available: {', '.join(_CONFIG_KEYS.keys())}"
        )
        return

    try:
        value = int(parts[1])
    except ValueError:
        await message.answer("Value must be a number!")
        return

    lo, hi, _desc = _CONFIG_KEYS[key]
    if value < lo or value > hi:
        await message.answer(f"Value must be between {lo} and {hi}!")
        return

    # Cross-validate min/max pairs
    if key == "timed_spawn_min":
        current_max = get_runtime_config("timed_spawn_max", _CONFIG_DEFAULTS["timed_spawn_max"])
        if value > current_max:
            await message.answer(f"timed_spawn_min ({value}) can't exceed timed_spawn_max ({current_max})!")
            return
    elif key == "timed_spawn_max":
        current_min = get_runtime_config("timed_spawn_min", _CONFIG_DEFAULTS["timed_spawn_min"])
        if value < current_min:
            await message.answer(f"timed_spawn_max ({value}) can't be less than timed_spawn_min ({current_min})!")
            return

    await _persist_config(session, key, value)
    await message.answer(
        f"✅ Set <b>{key}</b> = <b>{value}</b>\n"
        f"Saved to database — persists across restarts."
    )
    logger.info("Runtime config changed", key=key, value=value, by=message.from_user.id)


# ------------------------------------------------------------------ #
# /settings  — group admin command
# ------------------------------------------------------------------ #

@router.message(Command("settings"))
async def cmd_settings(message: Message, session: AsyncSession) -> None:
    """Handle /settings command for group admins."""
    if message.chat.type == "private":
        await message.answer("This command only works in groups!")
        return

    chat_member = await message.chat.get_member(message.from_user.id)
    if chat_member.status not in ("administrator", "creator"):
        await message.answer("Only group admins can use this command!")
        return

    result = await session.execute(
        select(Group).where(Group.chat_id == message.chat.id)
    )
    group = result.scalar_one_or_none()

    if not group:
        group = Group(
            chat_id=message.chat.id,
            title=message.chat.title,
        )
        session.add(group)
        await session.commit()

    settings_text = f"""
<b>Group Settings</b>
{esc(message.chat.title)}

<b>Spawning</b>
Enabled: {'Yes' if group.spawn_enabled else 'No'}
Spawn Threshold: {group.spawn_threshold} messages
Spawn Channel: {'Set' if group.spawn_channel_id else 'Not set'}

<b>Features</b>
Battles Enabled: {'Yes' if group.battles_enabled else 'No'}
Language: {group.language.upper()}

<b>Stats</b>
Total Spawns: {group.total_spawns}
Total Catches: {group.total_catches}

<i>Use inline buttons below to change settings.</i>
"""
    await message.answer(settings_text)


# ------------------------------------------------------------------ #
# /deregister  — reset a user to pre-starter state
# ------------------------------------------------------------------ #

@router.message(Command("deregister"))
async def cmd_deregister(message: Message, session: AsyncSession) -> None:
    """Delete all Pokemon for a user and reset their registration.

    Usage:
        /deregister <user_id>
        /deregister (reply to user's message)

    Bot owner + approved spawn admins can use this.
    """
    if not message.from_user:
        return

    user_id = message.from_user.id

    # Check access: bot owner OR spawn admin
    has_access = await is_spawn_admin(session, user_id)
    if not has_access:
        await message.answer("You don't have permission to use /deregister!")
        return

    # Determine target user
    parts = (message.text or "").split()
    target_user_id: int | None = None
    target_display = "Unknown"

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user_id = message.reply_to_message.from_user.id
        target_display = (
            message.reply_to_message.from_user.username
            or message.reply_to_message.from_user.full_name
            or str(target_user_id)
        )
    elif len(parts) >= 2:
        try:
            target_user_id = int(parts[1])
            target_display = str(target_user_id)
        except ValueError:
            await message.answer(
                "<b>Usage:</b>\n"
                "<code>/deregister [user_id]</code>\n"
                "Or reply to the user's message."
            )
            return

    if not target_user_id:
        await message.answer(
            "<b>Usage:</b>\n"
            "<code>/deregister [user_id]</code>\n"
            "Or reply to the user's message.\n\n"
            "This removes all Pokemon and resets the user so they can "
            "pick a new starter via /start."
        )
        return

    # Look up the target user record
    result = await session.execute(
        select(User).where(User.telegram_id == target_user_id)
    )
    target_user = result.scalar_one_or_none()

    if not target_user:
        await message.answer(
            f"User <code>{target_user_id}</code> not found in the database."
        )
        return

    # Count their Pokemon
    result = await session.execute(
        select(func.count(Pokemon.id)).where(Pokemon.owner_id == target_user_id)
    )
    pokemon_count = result.scalar() or 0

    if pokemon_count == 0:
        await message.answer(
            f"User <b>{target_display}</b> (<code>{target_user_id}</code>) "
            "has no Pokemon — already deregistered."
        )
        return

    # Delete all their Pokemon
    await session.execute(
        delete(Pokemon).where(Pokemon.owner_id == target_user_id)
    )

    # Reset selected pokemon
    target_user.selected_pokemon_id = None

    await session.commit()

    await message.answer(
        f"Deregistered <b>{target_display}</b> (<code>{target_user_id}</code>).\n\n"
        f"Removed <b>{pokemon_count}</b> Pokemon.\n"
        "They can now use /start to pick a new starter."
    )
    logger.info(
        "User deregistered",
        target_user_id=target_user_id,
        pokemon_removed=pokemon_count,
        deregistered_by=user_id,
    )


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _extract_target(message: Message) -> tuple[int | None, str]:
    """Extract target user_id and display name from reply or text_mention."""
    target_user_id = None
    target_username = "Unknown"

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user_id = message.reply_to_message.from_user.id
        target_username = (
            message.reply_to_message.from_user.username
            or message.reply_to_message.from_user.full_name
            or str(target_user_id)
        )
    elif message.entities:
        for entity in message.entities:
            if entity.type == "mention":
                mention_text = (message.text or "")[entity.offset : entity.offset + entity.length]
                return None, mention_text.lstrip("@")
            elif entity.type == "text_mention" and entity.user:
                target_user_id = entity.user.id
                target_username = entity.user.username or str(target_user_id)

    return target_user_id, target_username


# ------------------------------------------------------------------ #
# Reload emoji maps
# ------------------------------------------------------------------ #

@router.message(Command("reload_emoji"))
async def cmd_reload_emoji(message: Message) -> None:
    """Hot-reload all emoji map files without restarting the bot."""
    if not message.from_user or message.from_user.id != BOT_OWNER_ID:
        await message.answer("Only the bot owner can use this command!")
        return

    from telemon.core.emoji import reload_all_maps, mode_label

    counts = reload_all_maps()
    lines = [
        f"<b>Emoji maps reloaded</b> ({mode_label()})\n",
        f"Pokemon: {counts['pokemon']}",
        f"Forms: {counts['forms']}",
        f"Items: {counts['items']}",
        f"Stones: {counts['stones']}",
        f"Types: {counts['types']}",
    ]
    await message.answer("\n".join(lines))
    logger.info("Emoji maps reloaded", **counts)
