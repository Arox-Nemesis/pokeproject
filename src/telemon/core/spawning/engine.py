"""Spawning engine for wild Pokemon."""

import random
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import func

from telemon.config import settings
from telemon.core import regional, shiny
from telemon.database.models import ActiveSpawn, Group, PokemonSpecies
from telemon.logging import get_logger

logger = get_logger(__name__)


# Relative spawn weight of a SINGLE species in each rarity tier.
#
# These are per-species weights, not per-tier shares.  The chance of a tier
# appearing is derived at runtime as (species in tier) x (weight below), so a
# tier holding very few species can never become disproportionately common.
# The old implementation used fixed per-tier shares, which made the 3-species
# ultra_rare tier (Beldum/Metang/Metagross) the most frequent spawn in the game.
#
# Keep this list strictly descending: rarer tier => smaller per-species weight.
PER_SPECIES_WEIGHT = {
    "common": 100.0,     # catch_rate > 120
    "uncommon": 55.0,    # catch_rate 46-120
    "rare": 18.0,        # catch_rate 4-45
    "ultra_rare": 8.0,   # catch_rate 1-3
    "legendary": 2.5,    # is_legendary
    "mythical": 2.0,     # is_mythical
}

# How long the per-tier species counts stay cached, in seconds.  The species
# table is effectively static at runtime, so this only matters after an import.
_TIER_COUNT_TTL = 600

# Cache of (rarity -> number of eligible species), refreshed periodically.
_tier_counts: dict[str, int] | None = None
_tier_counts_at: datetime | None = None

# Only real dex entries spawn in the wild.  Megas, regional variants and other
# alternate forms are stored at national_dex >= 10000 and are excluded by this
# bound alone, so no name-based filtering is needed.
#
# NOTE: an earlier version also dropped any species whose name ended in a form
# suffix ("Normal", "Ordinary", "Land", ...).  For 34 Pokemon the suffixed row
# is the ONLY row below 10000 -- Keldeo exists solely as "Keldeo Ordinary" --
# so that filter made those species completely unobtainable in the wild.
MAX_WILD_DEX = 10000

# Chance that a spawn of a species with regional variants is replaced by one of
# those variants.  Regionals are pulled in through their BASE species' rarity
# tier rather than being given their own -- they have no catch_rate of their own
# worth trusting (the importer copies the base value), and routing them through
# the base keeps a rare base rare.  1-in-16 makes a regional roughly as
# uncommon as it should feel without making the base species hard to find.
REGIONAL_SPAWN_CHANCE = 1 / 16

# How long an uncaught spawn keeps its chat from spawning again.  This is NOT
# the flee timeout: when fleeing is disabled a spawn never expires, so without
# an upper bound here one un-guessed Pokemon silences a group permanently.
SPAWN_BLOCK_MINUTES = 30

# The same guard, but for incense.  Incense is a paid burst -- the loop ticks
# every 10 seconds -- so the 30-minute pacing window is completely wrong for
# it: a 120-spawn incense in a group where nobody guesses would take 60 hours
# to drain.  60 seconds still stops an unattended group from being carpeted
# with spawns nobody is answering, while an active group drains at loop speed.
INCENSE_BLOCK_SECONDS = 60


def _rarity_condition(rarity: str):
    """Build the SQL predicate selecting exactly one rarity tier.

    The tiers are mutually exclusive and together cover every species, matching
    PokemonSpecies.rarity.  Legendaries and mythicals are classified by flag
    regardless of catch_rate, which keeps species like Necrozma or Eternatus
    (catch_rate 255) out of the common pool.
    """
    is_legendary = PokemonSpecies.is_legendary.is_(True)
    is_mythical = PokemonSpecies.is_mythical.is_(True)
    ordinary = and_(
        PokemonSpecies.is_legendary.is_(False),
        PokemonSpecies.is_mythical.is_(False),
    )

    if rarity == "mythical":
        return is_mythical
    if rarity == "legendary":
        return and_(is_legendary, PokemonSpecies.is_mythical.is_(False))
    if rarity == "ultra_rare":
        return and_(ordinary, PokemonSpecies.catch_rate <= 3)
    if rarity == "rare":
        return and_(
            ordinary,
            PokemonSpecies.catch_rate > 3,
            PokemonSpecies.catch_rate <= 45,
        )
    if rarity == "uncommon":
        return and_(
            ordinary,
            PokemonSpecies.catch_rate > 45,
            PokemonSpecies.catch_rate <= 120,
        )
    return and_(ordinary, PokemonSpecies.catch_rate > 120)


async def _get_tier_counts(session: AsyncSession) -> dict[str, int]:
    """Return the number of spawnable species in each rarity tier (cached)."""
    global _tier_counts, _tier_counts_at

    now = datetime.utcnow()
    if (
        _tier_counts is not None
        and _tier_counts_at is not None
        and (now - _tier_counts_at).total_seconds() < _TIER_COUNT_TTL
    ):
        return _tier_counts

    counts: dict[str, int] = {}
    for rarity in PER_SPECIES_WEIGHT:
        result = await session.execute(
            select(func.count())
            .select_from(PokemonSpecies)
            .where(PokemonSpecies.national_dex < MAX_WILD_DEX)
            .where(_rarity_condition(rarity))
        )
        counts[rarity] = result.scalar() or 0

    _tier_counts = counts
    _tier_counts_at = now
    return counts


def invalidate_tier_cache() -> None:
    """Drop the cached tier counts (call after importing/altering species)."""
    global _tier_counts, _tier_counts_at
    _tier_counts = None
    _tier_counts_at = None


async def roll_rarity(session: AsyncSession) -> str:
    """Pick a rarity tier, weighted by per-species weight x tier population.

    Because each tier's share is proportional to how many species it holds, two
    species in the same tier remain equally likely AND a species in a rarer tier
    is always strictly less likely than one in a commoner tier -- regardless of
    how the tiers are sized.
    """
    counts = await _get_tier_counts(session)

    tiers = [r for r in PER_SPECIES_WEIGHT if counts.get(r, 0) > 0]
    if not tiers:
        return "common"

    weights = [PER_SPECIES_WEIGHT[r] * counts[r] for r in tiers]
    return random.choices(tiers, weights=weights, k=1)[0]


async def _maybe_regional(
    session: AsyncSession, species: PokemonSpecies
) -> PokemonSpecies:
    """Occasionally swap ``species`` for one of its regional variants.

    Regional forms live at dex >= 10000 and so can never be picked by the tier
    query directly.  Substituting after the pick is what makes them spawnable
    while keeping Megas (also >= 10000) excluded, since only dex numbers listed
    in ``core.regional`` are eligible.
    """
    forms = regional.get_forms_for_base(species.national_dex)
    if not forms:
        return species
    if random.random() >= REGIONAL_SPAWN_CHANCE:
        return species

    form = random.choice(forms)
    result = await session.execute(
        select(PokemonSpecies).where(PokemonSpecies.national_dex == form.dex)
    )
    variant = result.scalar_one_or_none()
    if variant is None:
        # Species table lacks the form row -- verify_against_db logs this at
        # startup; fall back to the base rather than dropping the spawn.
        logger.warning("Regional form row missing", dex=form.dex)
        return species
    return variant


async def get_random_species(session: AsyncSession) -> PokemonSpecies | None:
    """Get a random Pokemon species for a wild spawn.

    Two-stage weighted pick: choose a rarity tier (weighted so that per-species
    odds stay ordered by rarity), then choose uniformly within that tier.  A
    species that has regional variants may then be swapped for one of them.
    """
    selected_rarity = await roll_rarity(session)

    # Pick uniformly inside the tier, in the database, so we never load the
    # whole tier into memory just to discard all but one row.
    result = await session.execute(
        select(PokemonSpecies)
        .where(PokemonSpecies.national_dex < MAX_WILD_DEX)
        .where(_rarity_condition(selected_rarity))
        .order_by(func.random())
        .limit(1)
    )
    species = result.scalar_one_or_none()

    if species is not None:
        return await _maybe_regional(session, species)

    # Tier turned out to be empty (stale cache, or a partial species import).
    logger.warning("Rarity tier empty, falling back", rarity=selected_rarity)
    invalidate_tier_cache()

    fallback = await session.execute(
        select(PokemonSpecies)
        .where(PokemonSpecies.national_dex < MAX_WILD_DEX)
        .order_by(func.random())
        .limit(1)
    )
    species = fallback.scalar_one_or_none()
    if species is None:
        return None
    return await _maybe_regional(session, species)


def should_be_shiny(chain_bonus: int = 0) -> bool:
    """Determine if a spawn should be shiny.

    Delegates to ``core.shiny`` so the spawn roll, the catch-time hunter bonus
    and the odds table shown by ``/shinyhunt`` all read from one set of tiers.
    The spawn roll itself is charm-free: a spawn belongs to a whole group, so
    no single player's Shiny Charm applies here.  The catcher's charm and chain
    are folded in at catch time via ``shiny.bonus_probability``.
    """
    return random.randint(1, shiny.shiny_rate(chain_bonus)) == 1


async def create_spawn(
    session: AsyncSession,
    chat_id: int,
    message_id: int,
    species: PokemonSpecies | None = None,
    force_shiny: bool = False,
    force_stats: dict | None = None,
) -> ActiveSpawn | None:
    """Create a new Pokemon spawn in a chat."""
    # Get random species if not provided
    if species is None:
        species = await get_random_species(session)
        if species is None:
            logger.warning("No Pokemon species found in database")
            return None

    # Determine shiny status
    is_shiny = force_shiny or should_be_shiny()

    # Determine expiration based on flee toggle
    from telemon.bot.handlers.admin import get_runtime_config

    flee_enabled = get_runtime_config("flee_enabled", 1)
    if flee_enabled:
        timeout = get_runtime_config("spawn_timeout", settings.spawn_timeout_seconds)
        expires_at = datetime.utcnow() + timedelta(seconds=timeout)
    else:
        expires_at = datetime.utcnow() + timedelta(days=365 * 100)

    # Create spawn
    spawn = ActiveSpawn(
        chat_id=chat_id,
        species_id=species.national_dex,
        message_id=message_id,
        is_shiny=is_shiny,
        expires_at=expires_at,
    )

    # Apply forced stats if provided
    if force_stats:
        spawn.force_level = force_stats.get("level")
        spawn.force_iv_hp = force_stats.get("iv_hp")
        spawn.force_iv_attack = force_stats.get("iv_attack")
        spawn.force_iv_defense = force_stats.get("iv_defense")
        spawn.force_iv_sp_attack = force_stats.get("iv_sp_attack")
        spawn.force_iv_sp_defense = force_stats.get("iv_sp_defense")
        spawn.force_iv_speed = force_stats.get("iv_speed")

    session.add(spawn)

    # Update group stats
    result = await session.execute(
        select(Group).where(Group.chat_id == chat_id)
    )
    group = result.scalar_one_or_none()

    if group:
        group.message_count = 0
        # Randomize next spawn threshold so users can't predict exact count
        group.spawn_threshold = random.randint(
            settings.spawn_threshold_min, settings.spawn_threshold_max
        )
        # Note: group.total_spawns and group.last_spawn_at are updated by
        # the caller only AFTER confirming the spawn message was delivered.

    # Flush but don't commit — caller controls the transaction
    await session.flush()

    logger.info(
        "Created spawn",
        chat_id=chat_id,
        species=species.name,
        is_shiny=is_shiny,
    )

    return spawn


async def get_active_spawn(
    session: AsyncSession, chat_id: int, *, incense: bool = False
) -> ActiveSpawn | None:
    """Get the spawn that should block a new spawn in ``chat_id``.

    Two rows are deliberately not treated as blocking:

    * ``message_id = 0`` -- never delivered to Telegram (the process died
      between ``create_spawn`` and the send).  Nobody can see it, let alone
      catch it.
    * anything older than the block window -- with ``flee_enabled = 0`` spawns
      are given a 100-year ``expires_at``, so a Pokemon nobody manages to guess
      would otherwise block its chat's spawning *forever*.  409 live groups
      were stuck this way, several of them for over a month.

    Pass ``incense=True`` from the incense loop: incense is a paid burst that
    ticks every 10 seconds, so it uses ``INCENSE_BLOCK_SECONDS`` instead of the
    much longer natural-pacing window.

    A never-guessed spawn stays catchable (``catch.py`` filters on
    ``expires_at`` and takes the newest row), it just stops holding the queue.
    """
    if incense:
        cutoff = datetime.utcnow() - timedelta(seconds=INCENSE_BLOCK_SECONDS)
    else:
        cutoff = datetime.utcnow() - timedelta(minutes=SPAWN_BLOCK_MINUTES)
    result = await session.execute(
        select(ActiveSpawn)
        .where(ActiveSpawn.chat_id == chat_id)
        .where(ActiveSpawn.caught_by.is_(None))
        .where(ActiveSpawn.message_id != 0)
        .where(ActiveSpawn.expires_at > datetime.utcnow())
        .where(ActiveSpawn.spawned_at > cutoff)
        .order_by(ActiveSpawn.spawned_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def purge_undelivered_spawns(session: AsyncSession) -> int:
    """Delete spawn rows that never got a Telegram message, return the count.

    ``create_spawn`` inserts with ``message_id = 0`` and the caller patches in
    the real id once the send succeeds.  A crash, a restart or an unhandled
    send error in between leaves a row nobody can ever catch.  Run at startup
    so those rows do not accumulate.
    """
    result = await session.execute(
        delete(ActiveSpawn)
        .where(ActiveSpawn.message_id == 0)
        .where(ActiveSpawn.caught_by.is_(None))
    )
    await session.commit()
    count = result.rowcount or 0
    if count:
        logger.info("Purged undelivered spawns", count=count)
    return count


async def check_spawn_trigger(session: AsyncSession, chat_id: int) -> bool:
    """Check if a spawn should be triggered for a chat."""
    # Get group settings
    result = await session.execute(
        select(Group).where(Group.chat_id == chat_id)
    )
    group = result.scalar_one_or_none()

    if not group or not group.spawn_enabled:
        return False

    # Check if there's already an active spawn
    active_spawn = await get_active_spawn(session, chat_id)
    if active_spawn:
        return False

    # Check message threshold
    if group.message_count >= group.spawn_threshold:
        # Reset counter now so that if the caller's spawn fails,
        # we don't re-trigger on every subsequent message.
        group.message_count = 0
        await session.flush()
        return True

    # Check time-based spawn (if enough time has passed and some activity)
    if group.last_spawn_at and group.message_count > 5:
        time_since_last = datetime.utcnow() - group.last_spawn_at
        min_time = timedelta(minutes=settings.spawn_time_min_minutes)
        max_time = timedelta(minutes=settings.spawn_time_max_minutes)

        if time_since_last > min_time:
            # Random chance increases as we approach max time
            progress = min(1.0, time_since_last / max_time)
            if random.random() < progress * 0.3:  # Up to 30% chance
                return True

    return False
