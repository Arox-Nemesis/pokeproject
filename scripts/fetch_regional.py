#!/usr/bin/env python3
"""Fetch regional Pokemon form data from LOCAL PokeAPI CSV files.

Reads stats, types, abilities, etc. from data/pokeapi/data/v2/csv/
and appends regional forms to data/pokemon.json.
"""

import csv
import json
from pathlib import Path

# ──────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
CSV_DIR = DATA_DIR / "pokeapi" / "data" / "v2" / "csv"
POKEMON_JSON = DATA_DIR / "pokemon.json"

# ──────────────────────────────────────────────
# Regional form PokeAPI pokemon IDs
# ──────────────────────────────────────────────

REGIONAL_FORMS: dict[str, list[int]] = {
    "Alolan": [
        10091, 10092, 10100, 10101, 10102, 10103, 10104, 10105,
        10106, 10107, 10108, 10109, 10110, 10111, 10112, 10113,
        10114, 10115,
    ],
    "Galarian": [
        10161, 10162, 10163, 10164, 10165, 10166, 10167, 10168,
        10169, 10170, 10171, 10172, 10173, 10174, 10175, 10176,
        10177, 10179, 10180,
    ],
    "Hisuian": [
        10229, 10230, 10231, 10232, 10233, 10234, 10235, 10236,
        10237, 10238, 10239, 10240, 10241, 10242, 10243, 10244,
    ],
    "Paldean": [
        10250, 10251, 10252, 10253,
    ],
}

ALL_REGIONAL_IDS = set()
ID_TO_REGION: dict[int, str] = {}
for region, ids in REGIONAL_FORMS.items():
    for pid in ids:
        ALL_REGIONAL_IDS.add(pid)
        ID_TO_REGION[pid] = region

REGION_GEN = {"Alolan": 7, "Galarian": 8, "Hisuian": 8, "Paldean": 9}

# ──────────────────────────────────────────────
# Name formatting
# ──────────────────────────────────────────────

_NAME_OVERRIDES = {
    "mr-mime-galar": "Galarian Mr. Mime",
    "farfetchd-galar": "Galarian Farfetch'd",
    "darmanitan-galar-standard": "Galarian Darmanitan",
    "tauros-paldea-combat-breed": "Paldean Tauros (Combat)",
    "tauros-paldea-blaze-breed": "Paldean Tauros (Blaze)",
    "tauros-paldea-aqua-breed": "Paldean Tauros (Aqua)",
}

_REGION_SUFFIXES = {
    "-alola": "Alolan",
    "-galar": "Galarian",
    "-hisui": "Hisuian",
    "-paldea": "Paldean",
}


def format_regional_name(identifier: str) -> tuple[str, str]:
    """Convert PokeAPI identifier → (display_name, name_lower)."""
    if identifier in _NAME_OVERRIDES:
        name = _NAME_OVERRIDES[identifier]
        return name, name.lower()

    for suffix, prefix in _REGION_SUFFIXES.items():
        if suffix in identifier:
            base = identifier.split(suffix)[0].replace("-", " ").title()
            name = f"{prefix} {base}"
            return name, name.lower()

    name = identifier.replace("-", " ").title()
    return name, name.lower()


# ──────────────────────────────────────────────
# CSV loaders
# ──────────────────────────────────────────────

def load_csv(filename: str) -> list[dict]:
    """Load a CSV file from the PokeAPI data directory."""
    with open(CSV_DIR / filename, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_lookup_tables():
    """Build all lookup tables from local CSVs."""
    # Types: id → name
    types_map = {int(r["id"]): r["identifier"] for r in load_csv("types.csv")}

    # Abilities: id → name
    abilities_map = {
        int(r["id"]): r["identifier"].replace("-", " ").title()
        for r in load_csv("abilities.csv")
    }

    # Growth rates: id → name
    growth_map = {int(r["id"]): r["identifier"] for r in load_csv("growth_rates.csv")}

    # Egg groups: id → name
    egg_map = {int(r["id"]): r["identifier"] for r in load_csv("egg_groups.csv")}

    # Pokemon base table: id → {species_id, identifier, height, weight, base_experience}
    pokemon_base = {}
    for r in load_csv("pokemon.csv"):
        pid = int(r["id"])
        if pid in ALL_REGIONAL_IDS:
            pokemon_base[pid] = {
                "species_id": int(r["species_id"]),
                "identifier": r["identifier"],
                "height": int(r["height"]),
                "weight": int(r["weight"]),
                "base_experience": int(r["base_experience"]) if r["base_experience"] else 64,
            }

    # Stats: pokemon_id → {stat_id: base_stat}
    # stat_id: 1=hp, 2=attack, 3=defense, 4=sp-attack, 5=sp-defense, 6=speed
    pokemon_stats: dict[int, dict[int, int]] = {}
    for r in load_csv("pokemon_stats.csv"):
        pid = int(r["pokemon_id"])
        if pid in ALL_REGIONAL_IDS:
            pokemon_stats.setdefault(pid, {})[int(r["stat_id"])] = int(r["base_stat"])

    # Types: pokemon_id → [(slot, type_name)]
    pokemon_types: dict[int, list[tuple[int, str]]] = {}
    for r in load_csv("pokemon_types.csv"):
        pid = int(r["pokemon_id"])
        if pid in ALL_REGIONAL_IDS:
            pokemon_types.setdefault(pid, []).append(
                (int(r["slot"]), types_map.get(int(r["type_id"]), "normal"))
            )

    # Abilities: pokemon_id → {abilities: [...], hidden: ...}
    pokemon_abilities: dict[int, dict] = {}
    for r in load_csv("pokemon_abilities.csv"):
        pid = int(r["pokemon_id"])
        if pid in ALL_REGIONAL_IDS:
            entry = pokemon_abilities.setdefault(pid, {"abilities": [], "hidden": None})
            aname = abilities_map.get(int(r["ability_id"]), "Unknown")
            if r["is_hidden"] == "1":
                entry["hidden"] = aname
            else:
                entry["abilities"].append(aname)

    # Species: species_id → {catch_rate, happiness, gender_rate, growth_rate, egg_groups, ...}
    species_data: dict[int, dict] = {}
    # Egg group membership: species_id → [egg_group_name]
    species_eggs: dict[int, list[str]] = {}
    for r in load_csv("pokemon_egg_groups.csv") if (CSV_DIR / "pokemon_egg_groups.csv").exists() else []:
        sid = int(r["species_id"])
        species_eggs.setdefault(sid, []).append(egg_map.get(int(r["egg_group_id"]), "unknown"))

    for r in load_csv("pokemon_species.csv"):
        sid = int(r["id"])
        species_data[sid] = {
            "capture_rate": int(r["capture_rate"]),
            "base_happiness": int(r["base_happiness"]) if r["base_happiness"] else 70,
            "gender_rate": int(r["gender_rate"]) if r["gender_rate"] else -1,
            "growth_rate_id": int(r["growth_rate_id"]),
            "is_legendary": r["is_legendary"] == "1",
            "is_mythical": r["is_mythical"] == "1",
            "is_baby": r["is_baby"] == "1",
            "egg_groups": species_eggs.get(sid, []),
        }

    return pokemon_base, pokemon_stats, pokemon_types, pokemon_abilities, species_data, growth_map


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    """Build regional form entries from local CSVs and append to pokemon.json."""
    print("Loading lookup tables from local CSVs...")
    pokemon_base, pokemon_stats, pokemon_types, pokemon_abilities, species_data, growth_map = (
        build_lookup_tables()
    )

    # Load existing pokemon.json
    with open(POKEMON_JSON) as f:
        existing = json.load(f)

    existing_ids = {p["national_dex"] for p in existing}
    print(f"Existing Pokemon: {len(existing)}")

    new_entries = []
    for pid in sorted(ALL_REGIONAL_IDS):
        if pid in existing_ids:
            continue

        base = pokemon_base.get(pid)
        if not base:
            print(f"  ✗ #{pid} — not found in pokemon.csv")
            continue

        region = ID_TO_REGION[pid]
        sid = base["species_id"]
        sp = species_data.get(sid, {})
        stats = pokemon_stats.get(pid, {})

        # Types (sorted by slot)
        types_list = sorted(pokemon_types.get(pid, []), key=lambda t: t[0])
        type1 = types_list[0][1] if types_list else "normal"
        type2 = types_list[1][1] if len(types_list) > 1 else None

        # Abilities
        ab = pokemon_abilities.get(pid, {"abilities": [], "hidden": None})

        # Name
        display_name, name_lower = format_regional_name(base["identifier"])

        # Gender ratio
        gr = sp.get("gender_rate", -1)
        gender_ratio = (8 - gr) * 12.5 if gr >= 0 else None

        entry = {
            "national_dex": pid,
            "name": display_name,
            "name_lower": name_lower,
            "type1": type1,
            "type2": type2,
            "base_hp": stats.get(1, 50),
            "base_attack": stats.get(2, 50),
            "base_defense": stats.get(3, 50),
            "base_sp_attack": stats.get(4, 50),
            "base_sp_defense": stats.get(5, 50),
            "base_speed": stats.get(6, 50),
            "abilities": ab["abilities"],
            "hidden_ability": ab["hidden"],
            "catch_rate": sp.get("capture_rate", 45),
            "base_friendship": sp.get("base_happiness", 70),
            "base_experience": base["base_experience"],
            "growth_rate": growth_map.get(sp.get("growth_rate_id", 2), "medium"),
            "gender_ratio": gender_ratio,
            "egg_groups": sp.get("egg_groups", []),
            "evolution_chain_id": None,
            "sprite_url": f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/{pid}.png",
            "sprite_shiny_url": f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/shiny/{pid}.png",
            "generation": REGION_GEN[region],
            "is_legendary": sp.get("is_legendary", False),
            "is_mythical": sp.get("is_mythical", False),
            "is_baby": False,
            "height": base["height"],
            "weight": base["weight"],
        }

        new_entries.append(entry)
        print(f"  ✓ #{pid} {display_name} ({type1}/{type2 or '—'})")

    if not new_entries:
        print("All regional forms already in pokemon.json!")
        return

    # Append and sort
    existing.extend(new_entries)
    existing.sort(key=lambda p: p["national_dex"])

    with open(POKEMON_JSON, "w") as f:
        json.dump(existing, f, indent=2)

    print(f"\nAdded {len(new_entries)} regional forms → {len(existing)} total Pokemon")
    for region, ids in REGIONAL_FORMS.items():
        count = sum(1 for e in new_entries if e["national_dex"] in set(ids))
        print(f"  {region}: {count}")


if __name__ == "__main__":
    main()
