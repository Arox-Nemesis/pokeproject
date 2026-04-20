"""Pokemon Forms & Mega Evolution system.

Loads mega evolution data from data/mega_evolutions.json and provides
helpers for battle integration:

- Check if a Pokemon can mega evolve (species + held mega stone match)
- Get the mega form data (overridden stats, types, ability)
- Apply mega evolution to a PvE participant (stat recalculation)
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telemon.logging import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────
# Load mega evolution data (once at import time)
# ──────────────────────────────────────────────

_MEGA_DATA_PATH = Path(__file__).parent.parent.parent.parent / "data" / "mega_evolutions.json"

# species_id -> list of mega forms (most species have 1, Charizard/Mewtwo have 2)
_MEGA_BY_SPECIES: dict[int, list[dict[str, Any]]] = {}

# mega_stone name_lower -> mega form data
_MEGA_BY_STONE: dict[str, dict[str, Any]] = {}

# All mega-capable species IDs
MEGA_CAPABLE_SPECIES: set[int] = set()


def _load_mega_data() -> None:
    """Load mega evolution data from JSON."""
    global _MEGA_BY_SPECIES, _MEGA_BY_STONE, MEGA_CAPABLE_SPECIES

    if not _MEGA_DATA_PATH.exists():
        logger.warning("Mega evolution data file not found", path=str(_MEGA_DATA_PATH))
        return

    with open(_MEGA_DATA_PATH) as f:
        data = json.load(f)

    for entry in data.get("mega_evolutions", []):
        species_id = entry["species_id"]
        MEGA_CAPABLE_SPECIES.add(species_id)

        if species_id not in _MEGA_BY_SPECIES:
            _MEGA_BY_SPECIES[species_id] = []
        _MEGA_BY_SPECIES[species_id].append(entry)

        # Index by stone name (skip Rayquaza which has no stone)
        stone = entry.get("mega_stone")
        if stone:
            _MEGA_BY_STONE[stone] = entry

    logger.info(
        "Loaded mega evolution data",
        species_count=len(MEGA_CAPABLE_SPECIES),
        total_forms=sum(len(v) for v in _MEGA_BY_SPECIES.values()),
    )


# Load on import
_load_mega_data()


# ──────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────

@dataclass
class MegaForm:
    """Resolved mega evolution form with stat overrides."""
    species_id: int
    species_name: str
    form_name: str
    mega_dex: int  # PokeAPI form ID (10033-10090)
    mega_stone: str | None
    mega_stone_display: str | None
    type1: str
    type2: str | None
    ability: str
    base_hp: int
    base_attack: int
    base_defense: int
    base_sp_attack: int
    base_sp_defense: int
    base_speed: int


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def get_mega_forms(species_id: int) -> list[MegaForm]:
    """Get all mega forms available for a species."""
    entries = _MEGA_BY_SPECIES.get(species_id, [])
    return [_entry_to_form(e) for e in entries]


def get_mega_form_for_stone(stone_name_lower: str) -> MegaForm | None:
    """Get the mega form triggered by a specific mega stone (held item name)."""
    entry = _MEGA_BY_STONE.get(stone_name_lower)
    if entry:
        return _entry_to_form(entry)
    return None


def can_mega_evolve(species_id: int, held_item_lower: str | None) -> MegaForm | None:
    """Check if a Pokemon with this species and held item can mega evolve.

    Returns the MegaForm if eligible, None otherwise.
    For Rayquaza: check for Dragon Ascent in moves instead (handled separately).
    """
    if species_id not in MEGA_CAPABLE_SPECIES:
        return None

    if not held_item_lower:
        return None

    # Look up mega stone
    form = get_mega_form_for_stone(held_item_lower)
    if form and form.species_id == species_id:
        return form

    return None


def can_rayquaza_mega(species_id: int, moves: list[str] | None) -> MegaForm | None:
    """Special check for Rayquaza mega (requires Dragon Ascent move, no stone)."""
    if species_id != 384:
        return None
    if not moves:
        return None

    move_names = [m.lower() for m in moves]
    if "dragon ascent" in move_names:
        entries = _MEGA_BY_SPECIES.get(384, [])
        if entries:
            return _entry_to_form(entries[0])
    return None


def get_all_mega_species() -> list[dict[str, Any]]:
    """Get list of all species that can mega evolve, with their form names and stones."""
    result = []
    for species_id, entries in sorted(_MEGA_BY_SPECIES.items()):
        for entry in entries:
            result.append({
                "species_id": species_id,
                "species_name": entry["species_name"],
                "form_name": entry["form_name"],
                "mega_dex": entry.get("mega_dex"),
                "mega_stone": entry.get("mega_stone"),
                "mega_stone_display": entry.get("mega_stone_display"),
            })
    return result


def get_base_species_for_mega_dex(mega_dex: int) -> tuple[int, str | None] | None:
    """Reverse lookup: mega dex number → (base_species_id, mega_stone_name_lower).

    Returns None if the dex number is not a known mega form.
    """
    for entries in _MEGA_BY_SPECIES.values():
        for entry in entries:
            if entry.get("mega_dex") == mega_dex:
                return entry["species_id"], entry.get("mega_stone")
    return None


def get_mega_forms_for_species(species_id: int) -> list[MegaForm]:
    """Get all MegaForm entries for a base species ID."""
    return get_mega_forms(species_id)


def apply_mega_to_pve_participant(
    participant_dict: dict,
    mega_form: MegaForm,
    level: int,
) -> dict:
    """Apply mega evolution stat overrides to a PvE participant dict (in-place).

    Recalculates stats from the mega form's base stats using the standard
    Pokemon stat formula. Returns the mutated dict for convenience.
    """
    from telemon.core.battle import calculate_stat

    # Use fixed IVs for the participant (PvE uses 15 for wild, 20 for NPC,
    # but for the player we pass the actual computed stats — so this function
    # is only for cases where we need to recompute from base stats).
    # For player Pokemon we need IV/EV data which we get from the pokemon object.
    # So this function is for "simple" recomputation from base stats with fixed IVs.

    # We assume the participant dict already has an "iv_value" or we default to 15
    iv_value = participant_dict.get("_iv_value", 15)

    participant_dict["name"] = mega_form.form_name
    participant_dict["type1"] = mega_form.type1
    participant_dict["type2"] = mega_form.type2
    participant_dict["ability"] = mega_form.ability.lower()

    # Recalc stats
    participant_dict["hp"] = calculate_stat(mega_form.base_hp, iv_value, 0, level, is_hp=True)
    participant_dict["max_hp"] = participant_dict["hp"]
    participant_dict["attack"] = calculate_stat(mega_form.base_attack, iv_value, 0, level)
    participant_dict["defense"] = calculate_stat(mega_form.base_defense, iv_value, 0, level)
    participant_dict["sp_attack"] = calculate_stat(mega_form.base_sp_attack, iv_value, 0, level)
    participant_dict["sp_defense"] = calculate_stat(mega_form.base_sp_defense, iv_value, 0, level)
    participant_dict["speed"] = calculate_stat(mega_form.base_speed, iv_value, 0, level)

    return participant_dict


def apply_mega_to_player_participant(
    participant_dict: dict,
    mega_form: MegaForm,
    level: int,
    iv_hp: int, iv_atk: int, iv_def: int,
    iv_spa: int, iv_spd: int, iv_spe: int,
    ev_hp: int, ev_atk: int, ev_def: int,
    ev_spa: int, ev_spd: int, ev_spe: int,
) -> dict:
    """Apply mega evolution stat overrides to a player's participant dict.

    Uses the player's real IVs and EVs for accurate stat recomputation.
    Keeps current HP ratio to avoid unfair healing.
    """
    from telemon.core.battle import calculate_stat

    # Preserve HP ratio
    old_hp = participant_dict["hp"]
    old_max = participant_dict["max_hp"]
    hp_ratio = old_hp / old_max if old_max > 0 else 1.0

    participant_dict["name"] = mega_form.form_name
    participant_dict["type1"] = mega_form.type1
    participant_dict["type2"] = mega_form.type2
    participant_dict["ability"] = mega_form.ability.lower()

    new_max_hp = calculate_stat(mega_form.base_hp, iv_hp, ev_hp, level, is_hp=True)
    participant_dict["max_hp"] = new_max_hp
    participant_dict["hp"] = max(1, int(new_max_hp * hp_ratio))

    participant_dict["attack"] = calculate_stat(mega_form.base_attack, iv_atk, ev_atk, level)
    participant_dict["defense"] = calculate_stat(mega_form.base_defense, iv_def, ev_def, level)
    participant_dict["sp_attack"] = calculate_stat(mega_form.base_sp_attack, iv_spa, ev_spa, level)
    participant_dict["sp_defense"] = calculate_stat(mega_form.base_sp_defense, iv_spd, ev_spd, level)
    participant_dict["speed"] = calculate_stat(mega_form.base_speed, iv_spe, ev_spe, level)

    return participant_dict


# ──────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────

def _entry_to_form(entry: dict[str, Any]) -> MegaForm:
    return MegaForm(
        species_id=entry["species_id"],
        species_name=entry["species_name"],
        form_name=entry["form_name"],
        mega_dex=entry.get("mega_dex", 0),
        mega_stone=entry.get("mega_stone"),
        mega_stone_display=entry.get("mega_stone_display"),
        type1=entry["type1"],
        type2=entry.get("type2"),
        ability=entry["ability"],
        base_hp=entry["base_hp"],
        base_attack=entry["base_attack"],
        base_defense=entry["base_defense"],
        base_sp_attack=entry["base_sp_attack"],
        base_sp_defense=entry["base_sp_defense"],
        base_speed=entry["base_speed"],
    )
