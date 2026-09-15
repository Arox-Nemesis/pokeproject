"""Evolution system for Pokemon."""

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telemon.core import regional
from telemon.core.items import ITEM_BY_NAME, LINKING_CORD_ID
from telemon.database.models import InventoryItem, Pokemon, PokemonSpecies
from telemon.logging import get_logger

logger = get_logger(__name__)

# Load evolution data
_EVOLUTION_DATA: dict[str, Any] = {}

# data/evolutions.json is generated from PokeAPI, which records these as
# special-condition evolutions (Sirfetch'd needs three critical hits, Gholdengo
# needs 999 coins, ...) with no minimum level.  Without a level the "level"
# trigger defaults to 1 and the Pokemon evolves the moment it is caught, so each
# one gets the level it is realistically reachable at instead.
_MIN_LEVEL_FALLBACKS: dict[tuple[int, int], int] = {
    (83, 865): 30,  # Farfetch'd -> Sirfetch'd (3 crits in one battle)
    (211, 904): 30,  # Qwilfish -> Overqwil (Barb Barrage x20)
    (234, 899): 30,  # Stantler -> Wyrdeer (Psyshield Bash x20)
    (290, 292): 20,  # Nincada -> Shedinja (spare party slot at Ninjask's level)
    (550, 902): 30,  # Basculin -> Basculegion (recoil damage)
    (562, 867): 34,  # Yamask -> Runerigus (damage + Dusty Bowl)
    (625, 983): 52,  # Bisharp -> Kingambit (defeat 3 Bisharp leaders)
    (868, 869): 30,  # Milcery -> Alcremie (spin with a sweet)
    (891, 892): 40,  # Kubfu -> Urshifu (Tower of Darkness/Waters)
    (924, 925): 25,  # Tandemaus -> Maushold (canon level 25)
    (999, 1000): 45,  # Gimmighoul -> Gholdengo (999 coins)
}


def _evo_key(evo: dict[str, Any]) -> tuple:
    """Identity of an evolution entry, for duplicate removal."""
    return (
        evo.get("species_id"),
        evo.get("evolves_to"),
        evo.get("trigger"),
        evo.get("min_level"),
        evo.get("item"),
        evo.get("min_friendship"),
        evo.get("target_form"),
        evo.get("held_item"),
        evo.get("min_battles"),
        evo.get("time"),
    )


def _dedupe(entries) -> list[dict[str, Any]]:
    """Keep the first occurrence of each distinct evolution entry."""
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for evo in entries:
        key = _evo_key(evo)
        if key in seen:
            continue
        seen.add(key)
        out.append(evo)
    return out


def _normalize_chain(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate entries and supply missing ``min_level`` values.

    The generated data lists some transitions twice (every Eevee stone route,
    the Gen-1 water stone lines), which would otherwise show up twice in the
    "Possible Evolutions" display.
    """
    filled = []
    for evo in chain:
        if evo.get("trigger") == "level" and "min_level" not in evo:
            fallback = _MIN_LEVEL_FALLBACKS.get((evo["species_id"], evo["evolves_to"]))
            if fallback is not None:
                evo = {**evo, "min_level": fallback}
        filled.append(evo)
    return _dedupe(filled)


def _load_evolution_data() -> dict[str, Any]:
    """Load evolution data from JSON file."""
    global _EVOLUTION_DATA
    if _EVOLUTION_DATA:
        return _EVOLUTION_DATA

    data_path = Path(__file__).parent.parent.parent.parent.parent / "data" / "evolutions.json"
    if data_path.exists():
        with open(data_path) as f:
            raw = json.load(f)
        _EVOLUTION_DATA = {
            chain_id: {**chain_data, "chain": _normalize_chain(chain_data.get("chain", []))}
            for chain_id, chain_data in raw.items()
        }
        still_missing = sum(
            1
            for chain_data in _EVOLUTION_DATA.values()
            for evo in chain_data["chain"]
            if evo.get("trigger") == "level" and "min_level" not in evo
        )
        if still_missing:
            logger.warning(
                "Level evolutions with no min_level would trigger at level 1",
                count=still_missing,
            )
    else:
        logger.warning("Evolution data file not found", path=str(data_path))
        _EVOLUTION_DATA = {}

    return _EVOLUTION_DATA


def get_evolution_data() -> dict[str, Any]:
    """Get evolution data."""
    return _load_evolution_data()


def _evolutions_for_species(species_id: int) -> list[dict[str, Any]]:
    """Every evolution entry available to ``species_id``.

    Regional forms are not in ``data/evolutions.json`` at all, so their single
    line comes from ``core.regional``.  Base species get their generated entries
    minus the ones that are only legal from a regional form.
    """
    form_evo = regional.get_evolution(species_id)
    if form_evo:
        entries: list[dict[str, Any]] = []
        for target, trigger, requirement in form_evo:
            entry: dict[str, Any] = {
                "species_id": species_id,
                "evolves_to": target,
                "trigger": trigger,
            }
            if trigger == "level":
                entry["min_level"] = requirement
            elif trigger == "item":
                entry["item"] = requirement
            entries.append(entry)
        return entries

    if regional.is_regional(species_id):
        # A regional form with no evolution of its own (e.g. Alolan Ninetales).
        return []

    return _dedupe(
        evo
        for chain_data in get_evolution_data().values()
        for evo in chain_data.get("chain", [])
        if evo["species_id"] == species_id
        and not regional.is_form_exclusive_evolution(species_id, evo["evolves_to"])
    )


class EvolutionResult:
    """Result of an evolution check or attempt."""

    def __init__(
        self,
        can_evolve: bool,
        evolved_species_id: int | None = None,
        evolved_species_name: str | None = None,
        trigger: str | None = None,
        requirement: str | None = None,
        missing_requirement: str | None = None,
        evolved_form: str | None = None,
    ):
        self.can_evolve = can_evolve
        self.evolved_species_id = evolved_species_id
        self.evolved_species_name = evolved_species_name
        self.trigger = trigger
        self.requirement = requirement
        self.missing_requirement = missing_requirement
        self.evolved_form = evolved_form


def _is_satisfiable(
    evo: dict[str, Any],
    pokemon: Pokemon,
    use_item_lower: str | None,
    is_trade: bool,
) -> bool:
    """Whether this entry's condition holds right now, ignoring inventory.

    Used only to decide whether a branching evolution needs the player to pick;
    the authoritative check happens in the main loop of ``check_evolution``.
    """
    trigger = evo.get("trigger")
    if trigger == "level":
        return pokemon.level >= evo.get("min_level", 1)
    if trigger == "item":
        return bool(use_item_lower) and use_item_lower == evo.get("item", "").lower()
    if trigger == "trade":
        return is_trade
    if trigger == "friendship":
        return pokemon.friendship >= evo.get("min_friendship", 220)
    return False


async def _species_names(session: AsyncSession, dex_ids: list[int]) -> dict[int, str]:
    """Map dex numbers to display names in one query."""
    if not dex_ids:
        return {}
    result = await session.execute(
        select(PokemonSpecies.national_dex, PokemonSpecies.name).where(
            PokemonSpecies.national_dex.in_(dex_ids)
        )
    )
    return dict(result.all())


# Generalized form-evolution rules. Multiple rules may share one target
# species and differ only by form/conditions.
FORM_EVOLUTION_RULES: dict[int, tuple[dict[str, Any], ...]] = {
    744: (
        {"evolves_to": 745, "target_form": "midday", "min_level": 25, "time": "day"},
        {
            "evolves_to": 745, "target_form": "midnight", "min_level": 25,
            "time": "night", "held_item": "dusk rock", "min_battles": 20, "battle_at_night": True,
        },
        {"evolves_to": 745, "target_form": "dusk", "min_level": 25, "time": "dusk"},
    ),
}

def _evolution_time_period() -> str:
    """Return day/dusk/night using the configured local evolution timezone."""
    try:
        from telemon.config import settings
        tz_name = getattr(settings, "evolution_timezone", "UTC")
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now = datetime.utcnow()
    hour = now.hour
    if hour >= 20 or hour < 6:
        return "night"
    if 17 <= hour < 20 or 6 <= hour < 8:
        return "dusk"
    return "day"


def _custom_form_evolutions(species_id: int) -> list[dict[str, Any]]:
    return [dict(rule) for rule in FORM_EVOLUTION_RULES.get(species_id, ())]


def _custom_rule_matches(rule: dict[str, Any], pokemon: Pokemon, use_item_lower: str | None) -> tuple[bool, str | None]:
    if pokemon.level < rule.get("min_level", 1):
        return False, f"Needs to reach level {rule['min_level']} (currently {pokemon.level})"
    required_item = rule.get("held_item")
    if required_item and (pokemon.held_item or "").lower() != required_item.lower():
        return False, f"Pokemon must be holding {required_item.title()}"
    min_battles = rule.get("min_battles")
    if min_battles is not None and pokemon.battle_count < min_battles:
        return False, f"Needs at least {min_battles} battles (currently {pokemon.battle_count})"
    required_time = rule.get("time")
    if required_time and _evolution_time_period() != required_time:
        return False, f"This evolution requires {required_time}"
    if rule.get("battle_at_night") and not pokemon.last_battle_was_night:
        return False, "At least one completed battle must have taken place at night"
    return True, None


async def check_evolution(
    session: AsyncSession,
    pokemon: Pokemon,
    user_id: int,
    use_item: str | None = None,
    is_trade: bool = False,
    target_species_id: int | None = None,
) -> EvolutionResult:
    """
    Check if a Pokemon can evolve.

    Args:
        session: Database session
        pokemon: The Pokemon to check
        user_id: The owner's user ID
        use_item: Item name if trying to evolve with an item
        is_trade: Whether this is a trade evolution check
        target_species_id: Restrict the check to one branch of a branching
            evolution.  Without it, a species with several currently-satisfiable
            branches (Eevee, Nincada, Wurmple) reports the choice back instead of
            silently taking whichever the data file happens to list first.

    Returns:
        EvolutionResult with evolution details
    """
    species_id = pokemon.species_id

    # Data-driven branching form evolutions. These rules are evaluated before
    # the generic PokeAPI chain so a single species can have multiple forms.
    custom_rules = _custom_form_evolutions(species_id)
    if custom_rules:
        rules = custom_rules
        if target_species_id is not None:
            rules = [r for r in rules if r["evolves_to"] == target_species_id]
        if target_form is not None:
            rules = [r for r in rules if r.get("target_form") == target_form.lower()]
        matching = []
        failures = []
        for rule in rules:
            ok, missing = _custom_rule_matches(rule, pokemon, use_item_lower)
            if ok:
                matching.append(rule)
            elif missing:
                failures.append(missing)
        if matching:
            # If multiple forms are currently valid, require an explicit form
            # target instead of silently choosing one.
            if len(matching) > 1:
                options = ", ".join(r["target_form"].title() for r in matching)
                return EvolutionResult(False, evolved_species_id=745, evolved_species_name="Lycanroc", trigger="form", missing_requirement=f"Choose a form: {options}")
            rule = matching[0]
            target = rule["evolves_to"]
            result = await session.execute(select(PokemonSpecies).where(PokemonSpecies.national_dex == target))
            target_species = result.scalar_one_or_none()
            return EvolutionResult(True, target, target_species.name if target_species else "Lycanroc", "form", f"Level {rule['min_level']}+ + {rule.get('time', 'special')} conditions", evolved_form=rule["target_form"])
        # A requested branch can be identified by item/time even when it is not
        # currently valid; show all unmet requirements.
        return EvolutionResult(False, evolved_species_id=745, evolved_species_name="Lycanroc", trigger="form", missing_requirement="; ".join(dict.fromkeys(failures)) or "Form evolution requirements are not met.")

    # Special case: if the user is using a Linking Cord, treat as trade
    use_item_lower = use_item.lower().strip() if use_item else None
    using_linking_cord = use_item_lower == "linking cord"

    if using_linking_cord:
        is_trade = True
        # The linking cord doesn't act as a trade-item (e.g. metal coat),
        # it just triggers the trade itself. So clear use_item for the
        # trade-with-item check below and let it match "trade, item=none".
        use_item = None
        use_item_lower = None

    # Custom item route: Cosmoem must use the store-only Lunala Stone for
    # Lunala.  This deliberately replaces the ambiguous raw data branch.
    if species_id == 790 and use_item_lower == "lunala stone":
        if pokemon.level < 53:
            return EvolutionResult(
                can_evolve=False,
                evolved_species_id=792,
                evolved_species_name="Lunala",
                trigger="item",
                requirement="Level 53+ + Lunala Stone",
                missing_requirement=f"Needs to reach level 53 (currently {pokemon.level})",
            )
        return EvolutionResult(
            can_evolve=True,
            evolved_species_id=792,
            evolved_species_name="Lunala",
            trigger="item",
            requirement="Level 53+ + Lunala Stone",
        )

    # Find evolution chain for this species
    possible_evolutions = _evolutions_for_species(species_id)

    if not possible_evolutions:
        return EvolutionResult(
            can_evolve=False,
            missing_requirement="This Pokemon cannot evolve.",
        )

    if target_species_id is not None:
        possible_evolutions = [
            evo for evo in possible_evolutions if evo["evolves_to"] == target_species_id
        ]
        if not possible_evolutions:
            return EvolutionResult(
                can_evolve=False,
                missing_requirement="That is not one of this Pokemon's evolutions.",
            )
    elif len(possible_evolutions) > 1:
        # Ask which branch, but only when more than one is actually reachable
        # right now -- Eevee with a single Fire Stone should just evolve.
        reachable = [
            evo
            for evo in possible_evolutions
            if _is_satisfiable(evo, pokemon, use_item_lower, is_trade)
        ]
        if len(reachable) > 1:
            names = await _species_names(session, [e["evolves_to"] for e in reachable])
            options = ", ".join(names.get(e["evolves_to"], "?") for e in reachable)
            return EvolutionResult(
                can_evolve=False,
                trigger=reachable[0]["trigger"],
                missing_requirement=(
                    f"This Pokemon can evolve into several forms: {options}.\n"
                    f"Pick one, e.g. /evolve {names.get(reachable[0]['evolves_to'], '')}"
                ),
            )
        if reachable:
            possible_evolutions = reachable

    # Check each possible evolution
    for evo in possible_evolutions:
        trigger = evo["trigger"]
        evolves_to = evo["evolves_to"]

        # Get the evolved species info
        result = await session.execute(
            select(PokemonSpecies).where(PokemonSpecies.national_dex == evolves_to)
        )
        evolved_species = result.scalar_one_or_none()

        if not evolved_species:
            continue

        if trigger == "level":
            min_level = evo.get("min_level", 1)
            if pokemon.level >= min_level:
                return EvolutionResult(
                    can_evolve=True,
                    evolved_species_id=evolves_to,
                    evolved_species_name=evolved_species.name,
                    trigger="level",
                    requirement=f"Level {min_level}+",
                )
            else:
                return EvolutionResult(
                    can_evolve=False,
                    evolved_species_id=evolves_to,
                    evolved_species_name=evolved_species.name,
                    trigger="level",
                    requirement=f"Level {min_level}+",
                    missing_requirement=f"Needs to reach level {min_level} (currently {pokemon.level})",
                )

        elif trigger == "item":
            required_item = evo.get("item", "").lower()

            if use_item_lower and use_item_lower == required_item:
                return EvolutionResult(
                    can_evolve=True,
                    evolved_species_id=evolves_to,
                    evolved_species_name=evolved_species.name,
                    trigger="item",
                    requirement=required_item.title(),
                )
            elif use_item_lower:
                # Wrong item — try next evolution
                continue
            else:
                # No item specified — tell user what's needed
                item_data = ITEM_BY_NAME.get(required_item)
                has_item = False
                if item_data:
                    inv_result = await session.execute(
                        select(InventoryItem)
                        .where(InventoryItem.user_id == user_id)
                        .where(InventoryItem.item_id == item_data["id"])
                        .where(InventoryItem.quantity > 0)
                    )
                    has_item = inv_result.scalar_one_or_none() is not None

                return EvolutionResult(
                    can_evolve=False,
                    evolved_species_id=evolves_to,
                    evolved_species_name=evolved_species.name,
                    trigger="item",
                    requirement=required_item.title(),
                    missing_requirement=f"Requires {required_item.title()}"
                    + (
                        " (you have it! Use: /evolve [num] {})".format(required_item)
                        if has_item
                        else " (buy from /shop)"
                    ),
                )

        elif trigger == "trade":
            trade_item = evo.get("item")

            if is_trade:
                if trade_item and trade_item != "none":
                    # Trade evolution that also requires a held item
                    # When using Linking Cord, the user must also specify the item
                    if using_linking_cord:
                        # Check if user has the trade item
                        item_data = ITEM_BY_NAME.get(trade_item.lower())
                        has_item = False
                        if item_data:
                            inv_result = await session.execute(
                                select(InventoryItem)
                                .where(InventoryItem.user_id == user_id)
                                .where(InventoryItem.item_id == item_data["id"])
                                .where(InventoryItem.quantity > 0)
                            )
                            has_item = inv_result.scalar_one_or_none() is not None

                        if has_item:
                            return EvolutionResult(
                                can_evolve=True,
                                evolved_species_id=evolves_to,
                                evolved_species_name=evolved_species.name,
                                trigger="trade",
                                requirement=f"Trade + {trade_item.title()}",
                            )
                        else:
                            return EvolutionResult(
                                can_evolve=False,
                                evolved_species_id=evolves_to,
                                evolved_species_name=evolved_species.name,
                                trigger="trade",
                                requirement=f"Trade + {trade_item.title()}",
                                missing_requirement=f"Also requires {trade_item.title()} (buy from /shop)",
                            )
                    else:
                        # Real trade — check if the traded Pokemon holds the item
                        # For now, we let real trades evolve regardless of held item
                        return EvolutionResult(
                            can_evolve=True,
                            evolved_species_id=evolves_to,
                            evolved_species_name=evolved_species.name,
                            trigger="trade",
                            requirement=f"Trade + {trade_item.title()}",
                        )
                else:
                    # Simple trade evolution (no item needed)
                    return EvolutionResult(
                        can_evolve=True,
                        evolved_species_id=evolves_to,
                        evolved_species_name=evolved_species.name,
                        trigger="trade",
                        requirement="Trade",
                    )
            else:
                # Not trading — show requirement
                req = "Trade"
                if trade_item and trade_item != "none":
                    req += f" + {trade_item.title()}"

                # Check if user has a Linking Cord
                inv_result = await session.execute(
                    select(InventoryItem)
                    .where(InventoryItem.user_id == user_id)
                    .where(InventoryItem.item_id == LINKING_CORD_ID)
                    .where(InventoryItem.quantity > 0)
                )
                has_cord = inv_result.scalar_one_or_none() is not None

                hint = "Trade with another trainer"
                if has_cord:
                    hint += " or use: /evolve [num] linking cord"
                else:
                    hint += " or buy a Linking Cord from /shop"

                return EvolutionResult(
                    can_evolve=False,
                    evolved_species_id=evolves_to,
                    evolved_species_name=evolved_species.name,
                    trigger="trade",
                    requirement=req,
                    missing_requirement=hint,
                )

        elif trigger == "friendship":
            min_friendship = evo.get("min_friendship", 220)
            if pokemon.friendship >= min_friendship:
                return EvolutionResult(
                    can_evolve=True,
                    evolved_species_id=evolves_to,
                    evolved_species_name=evolved_species.name,
                    trigger="friendship",
                    requirement=f"Friendship {min_friendship}+",
                )
            else:
                return EvolutionResult(
                    can_evolve=False,
                    evolved_species_id=evolves_to,
                    evolved_species_name=evolved_species.name,
                    trigger="friendship",
                    requirement=f"Friendship {min_friendship}+",
                    missing_requirement=f"Needs {min_friendship} friendship (currently {pokemon.friendship}). Use /pet to increase!",
                )

    return EvolutionResult(
        can_evolve=False,
        missing_requirement="Evolution conditions not met.",
    )


async def evolve_pokemon(
    session: AsyncSession,
    pokemon: Pokemon,
    user_id: int,
    use_item: str | None = None,
    is_trade: bool = False,
    target_species_id: int | None = None,
    target_form: str | None = None,
) -> tuple[bool, str]:
    """
    Attempt to evolve a Pokemon.

    Args:
        session: Database session
        pokemon: The Pokemon to evolve
        user_id: The owner's user ID
        use_item: Item name if evolving with an item
        is_trade: Whether this is a trade evolution
        target_species_id: Which branch to take, for branching evolutions

    Returns:
        Tuple of (success, message)
    """
    use_item_lower = use_item.lower().strip() if use_item else None
    using_linking_cord = use_item_lower == "linking cord"

    # Check if can evolve
    result = await check_evolution(session, pokemon, user_id, use_item, is_trade, target_species_id, target_form)

    if not result.can_evolve:
        return False, result.missing_requirement or "Cannot evolve."

    # Get the evolved species
    species_result = await session.execute(
        select(PokemonSpecies).where(PokemonSpecies.national_dex == result.evolved_species_id)
    )
    evolved_species = species_result.scalar_one_or_none()

    if not evolved_species:
        return False, "Evolution target species not found."

    old_species_name = pokemon.species.name

    # Consume the item if used
    if result.trigger == "item" and use_item:
        item_data = ITEM_BY_NAME.get(use_item.lower().strip())
        if item_data:
            inv_result = await session.execute(
                select(InventoryItem)
                .where(InventoryItem.user_id == user_id)
                .where(InventoryItem.item_id == item_data["id"])
                .where(InventoryItem.quantity > 0)
            )
            inventory_item = inv_result.scalar_one_or_none()
            if inventory_item:
                inventory_item.quantity -= 1
            else:
                return False, f"You don't have a {use_item.title()}!"

    # If Linking Cord was used, consume it
    if using_linking_cord:
        inv_result = await session.execute(
            select(InventoryItem)
            .where(InventoryItem.user_id == user_id)
            .where(InventoryItem.item_id == LINKING_CORD_ID)
            .where(InventoryItem.quantity > 0)
        )
        cord_item = inv_result.scalar_one_or_none()
        if cord_item:
            cord_item.quantity -= 1
        else:
            return False, "You don't have a Linking Cord!"

        # If the trade evolution also needs an item, consume that too
        evolution_data = get_evolution_data()
        for chain_id, chain_data in evolution_data.items():
            for evo in chain_data.get("chain", []):
                if (
                    evo["species_id"] == pokemon.species_id
                    and evo["evolves_to"] == result.evolved_species_id
                ):
                    trade_item = evo.get("item")
                    if trade_item and trade_item != "none":
                        item_data = ITEM_BY_NAME.get(trade_item.lower())
                        if item_data:
                            inv_result = await session.execute(
                                select(InventoryItem)
                                .where(InventoryItem.user_id == user_id)
                                .where(InventoryItem.item_id == item_data["id"])
                                .where(InventoryItem.quantity > 0)
                            )
                            trade_inv = inv_result.scalar_one_or_none()
                            if trade_inv:
                                trade_inv.quantity -= 1
                            else:
                                return False, f"You don't have a {trade_item.title()}!"
                    break

    # Evolve the Pokemon
    pokemon.species_id = result.evolved_species_id

    # Pick new ability from evolved species
    import random

    if evolved_species.abilities:
        pokemon.ability = random.choice(evolved_species.abilities)

    # Learn anything the evolved form already knows at this level.  Without this
    # a Pokemon evolved past its starter moves keeps the pre-evolution's set
    # forever, since auto_learn_moves_on_levelup only fires on a level change.
    learned: list[str] = []
    try:
        from telemon.core.moves import MAX_MOVES, get_learnable_moves

        known = list(pokemon.moves or [])
        if len(known) < MAX_MOVES:
            learnable = await get_learnable_moves(session, pokemon.species_id, pokemon.level)
            # Highest-level moves first -- those are the ones the evolved form
            # gained access to.
            for entry in reversed(learnable):
                name = entry["move"].name_lower
                if name in known:
                    continue
                known.append(name)
                learned.append(entry["move"].name)
                if len(known) >= MAX_MOVES:
                    break
            if learned:
                pokemon.moves = known
    except Exception as e:
        logger.warning("Move refresh on evolve failed", error=str(e))

    # Record which alternate form this Pokemon is now in, so displays and future
    # lookups do not have to re-derive it from the species id.
    pokemon.form = result.evolved_form or (
        regional.get_form(pokemon.species_id).region
        if regional.is_regional(pokemon.species_id)
        else None
    )

    await session.commit()

    logger.info(
        "Pokemon evolved",
        pokemon_id=str(pokemon.id),
        from_species=old_species_name,
        to_species=evolved_species.name,
        trigger=result.trigger,
    )

    message = f"{old_species_name} evolved into {evolved_species.name}!"
    if learned:
        message += f"\nLearned {', '.join(learned)}!"
    return True, message


def get_possible_evolutions(species_id: int) -> list[dict]:
    """Get all generic and custom form evolutions for a species."""
    return _dedupe([*_evolutions_for_species(species_id), *_custom_form_evolutions(species_id)])
