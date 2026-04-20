#!/usr/bin/env python3
"""Fetch Mega Evolution Pokemon data from LOCAL PokeAPI CSV files.

Reads stats, types, abilities from data/pokeapi/data/v2/csv/
and appends 48 canonical Mega forms to data/pokemon.json.
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
# Canonical Mega Evolution PokeAPI pokemon IDs
# ──────────────────────────────────────────────

MEGA_IDS: list[int] = [
    10033,  # Mega Venusaur
    10034,  # Mega Charizard X
    10035,  # Mega Charizard Y
    10036,  # Mega Blastoise
    10037,  # Mega Alakazam
    10038,  # Mega Gengar
    10039,  # Mega Kangaskhan
    10040,  # Mega Pinsir
    10041,  # Mega Gyarados
    10042,  # Mega Aerodactyl
    10043,  # Mega Mewtwo X
    10044,  # Mega Mewtwo Y
    10045,  # Mega Ampharos
    10046,  # Mega Scizor
    10047,  # Mega Heracross
    10048,  # Mega Houndoom
    10049,  # Mega Tyranitar
    10050,  # Mega Blaziken
    10051,  # Mega Gardevoir
    10052,  # Mega Mawile
    10053,  # Mega Aggron
    10054,  # Mega Medicham
    10055,  # Mega Manectric
    10056,  # Mega Banette
    10057,  # Mega Absol
    10058,  # Mega Garchomp
    10059,  # Mega Lucario
    10060,  # Mega Abomasnow
    10062,  # Mega Latias
    10063,  # Mega Latios
    10064,  # Mega Swampert
    10065,  # Mega Sceptile
    10066,  # Mega Sableye
    10067,  # Mega Altaria
    10068,  # Mega Gallade
    10069,  # Mega Audino
    10070,  # Mega Sharpedo
    10071,  # Mega Slowbro
    10072,  # Mega Steelix
    10073,  # Mega Pidgeot
    10074,  # Mega Glalie
    10075,  # Mega Diancie
    10076,  # Mega Metagross
    10079,  # Mega Rayquaza
    10087,  # Mega Camerupt
    10088,  # Mega Lopunny
    10089,  # Mega Salamence
    10090,  # Mega Beedrill
]

MEGA_ID_SET = set(MEGA_IDS)

# ──────────────────────────────────────────────
# Name formatting
# ──────────────────────────────────────────────

_NAME_OVERRIDES = {
    "charizard-mega-x": "Mega Charizard X",
    "charizard-mega-y": "Mega Charizard Y",
    "mewtwo-mega-x": "Mega Mewtwo X",
    "mewtwo-mega-y": "Mega Mewtwo Y",
}


def format_mega_name(identifier: str) -> tuple[str, str]:
    """Convert PokeAPI identifier → (display_name, name_lower)."""
    if identifier in _NAME_OVERRIDES:
        name = _NAME_OVERRIDES[identifier]
        return name, name.lower()

    if "-mega" in identifier:
        base = identifier.split("-mega")[0].replace("-", " ").title()
        name = f"Mega {base}"
        return name, name.lower()

    name = identifier.replace("-", " ").title()
    return name, name.lower()


# ──────────────────────────────────────────────
# CSV loaders
# ──────────────────────────────────────────────

def load_csv(filename: str) -> list[dict]:
    with open(CSV_DIR / filename, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_lookup_tables():
    """Build lookup tables from local CSVs, filtered to Mega IDs."""
    types_map = {int(r["id"]): r["identifier"] for r in load_csv("types.csv")}
    abilities_map = {
        int(r["id"]): r["identifier"].replace("-", " ").title()
        for r in load_csv("abilities.csv")
    }
    growth_map = {int(r["id"]): r["identifier"] for r in load_csv("growth_rates.csv")}
    egg_map = {int(r["id"]): r["identifier"] for r in load_csv("egg_groups.csv")}

    pokemon_base = {}
    for r in load_csv("pokemon.csv"):
        pid = int(r["id"])
        if pid in MEGA_ID_SET:
            pokemon_base[pid] = {
                "species_id": int(r["species_id"]),
                "identifier": r["identifier"],
                "height": int(r["height"]),
                "weight": int(r["weight"]),
                "base_experience": int(r["base_experience"]) if r["base_experience"] else 64,
            }

    pokemon_stats: dict[int, dict[int, int]] = {}
    for r in load_csv("pokemon_stats.csv"):
        pid = int(r["pokemon_id"])
        if pid in MEGA_ID_SET:
            pokemon_stats.setdefault(pid, {})[int(r["stat_id"])] = int(r["base_stat"])

    pokemon_types: dict[int, list[tuple[int, str]]] = {}
    for r in load_csv("pokemon_types.csv"):
        pid = int(r["pokemon_id"])
        if pid in MEGA_ID_SET:
            pokemon_types.setdefault(pid, []).append(
                (int(r["slot"]), types_map.get(int(r["type_id"]), "normal"))
            )

    pokemon_abilities: dict[int, dict] = {}
    for r in load_csv("pokemon_abilities.csv"):
        pid = int(r["pokemon_id"])
        if pid in MEGA_ID_SET:
            entry = pokemon_abilities.setdefault(pid, {"abilities": [], "hidden": None})
            aname = abilities_map.get(int(r["ability_id"]), "Unknown")
            if r["is_hidden"] == "1":
                entry["hidden"] = aname
            else:
                entry["abilities"].append(aname)

    species_data: dict[int, dict] = {}
    species_eggs: dict[int, list[str]] = {}
    if (CSV_DIR / "pokemon_egg_groups.csv").exists():
        for r in load_csv("pokemon_egg_groups.csv"):
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


def main():
    """Build Mega form entries from local CSVs and append to pokemon.json."""
    print("Loading lookup tables from local CSVs...")
    pokemon_base, pokemon_stats, pokemon_types, pokemon_abilities, species_data, growth_map = (
        build_lookup_tables()
    )

    with open(POKEMON_JSON) as f:
        existing = json.load(f)

    existing_ids = {p["national_dex"] for p in existing}
    print(f"Existing Pokemon: {len(existing)}")

    new_entries = []
    for pid in sorted(MEGA_IDS):
        if pid in existing_ids:
            continue

        base = pokemon_base.get(pid)
        if not base:
            print(f"  ✗ #{pid} — not found in pokemon.csv")
            continue

        sid = base["species_id"]
        sp = species_data.get(sid, {})
        stats = pokemon_stats.get(pid, {})

        types_list = sorted(pokemon_types.get(pid, []), key=lambda t: t[0])
        type1 = types_list[0][1] if types_list else "normal"
        type2 = types_list[1][1] if len(types_list) > 1 else None

        ab = pokemon_abilities.get(pid, {"abilities": [], "hidden": None})
        display_name, name_lower = format_mega_name(base["identifier"])

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
            "generation": 6,  # Megas introduced in Gen 6
            "is_legendary": sp.get("is_legendary", False),
            "is_mythical": sp.get("is_mythical", False),
            "is_baby": False,
            "height": base["height"],
            "weight": base["weight"],
        }

        new_entries.append(entry)
        print(f"  ✓ #{pid} {display_name} ({type1}/{type2 or '—'})")

    if not new_entries:
        print("All Mega forms already in pokemon.json!")
        return

    existing.extend(new_entries)
    existing.sort(key=lambda p: p["national_dex"])

    with open(POKEMON_JSON, "w") as f:
        json.dump(existing, f, indent=2)

    print(f"\nAdded {len(new_entries)} Mega forms → {len(existing)} total Pokemon")


if __name__ == "__main__":
    main()
