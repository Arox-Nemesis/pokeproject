"""Regional variant forms (Alolan / Galarian / Hisuian / Paldean).

Regional forms live in ``pokemon_species`` at ``national_dex >= 10000``, the
same bucket as Mega forms, so the wild-spawn query excludes them by default.
This module holds the static base<->regional mapping plus the evolution rules
those forms need, and is the single source of truth for:

* which alternate-form rows may appear as a wild spawn (see
  ``telemon.core.spawning.engine``),
* which base species a form falls back to for learnsets (no ``pokemon_learnsets``
  row exists for any dex >= 10000),
* the regional evolution lines, which ``data/evolutions.json`` does not contain
  because it is generated from base-species chains only.

The dex numbers are PokeAPI form IDs and are stable across data re-imports;
``verify_against_db`` re-checks them at startup and logs any drift.
"""

from __future__ import annotations

from dataclasses import dataclass

from telemon.logging import get_logger

logger = get_logger(__name__)

# Canonical region names, in the order they were introduced.
REGIONS = ("alolan", "galarian", "hisuian", "paldean")

# Prefixes accepted in user input, mapped to the canonical region name.
REGION_ALIASES: dict[str, str] = {
    "alolan": "alolan",
    "alola": "alolan",
    "galarian": "galarian",
    "galar": "galarian",
    "hisuian": "hisuian",
    "hisui": "hisuian",
    "paldean": "paldean",
    "paldea": "paldean",
}


@dataclass(frozen=True)
class RegionalForm:
    """One regional variant row in ``pokemon_species``."""

    dex: int
    base_dex: int
    region: str
    name_lower: str

    @property
    def short_name(self) -> str:
        """The species name with the region prefix stripped ("vulpix")."""
        return self.name_lower.split(" ", 1)[1] if " " in self.name_lower else self.name_lower


# (form dex, base dex, region, name_lower).  Base dex numbers were resolved
# against pokemon_species; the six names that are not a plain prefix strip
# (Farfetch'd, Mr. Mime, Darmanitan -> darmanitan-standard, the three Paldean
# Tauros breeds -> tauros) are baked in here rather than derived.
_FORM_ROWS: tuple[tuple[int, int, str, str], ...] = (
    (10091, 19, "alolan", "alolan rattata"),
    (10092, 20, "alolan", "alolan raticate"),
    (10100, 26, "alolan", "alolan raichu"),
    (10101, 27, "alolan", "alolan sandshrew"),
    (10102, 28, "alolan", "alolan sandslash"),
    (10103, 37, "alolan", "alolan vulpix"),
    (10104, 38, "alolan", "alolan ninetales"),
    (10105, 50, "alolan", "alolan diglett"),
    (10106, 51, "alolan", "alolan dugtrio"),
    (10107, 52, "alolan", "alolan meowth"),
    (10108, 53, "alolan", "alolan persian"),
    (10109, 74, "alolan", "alolan geodude"),
    (10110, 75, "alolan", "alolan graveler"),
    (10111, 76, "alolan", "alolan golem"),
    (10112, 88, "alolan", "alolan grimer"),
    (10113, 89, "alolan", "alolan muk"),
    (10114, 103, "alolan", "alolan exeggutor"),
    (10115, 105, "alolan", "alolan marowak"),
    (10161, 52, "galarian", "galarian meowth"),
    (10162, 77, "galarian", "galarian ponyta"),
    (10163, 78, "galarian", "galarian rapidash"),
    (10164, 79, "galarian", "galarian slowpoke"),
    (10165, 80, "galarian", "galarian slowbro"),
    (10166, 83, "galarian", "galarian farfetch'd"),
    (10167, 110, "galarian", "galarian weezing"),
    (10168, 122, "galarian", "galarian mr. mime"),
    (10169, 144, "galarian", "galarian articuno"),
    (10170, 145, "galarian", "galarian zapdos"),
    (10171, 146, "galarian", "galarian moltres"),
    (10172, 199, "galarian", "galarian slowking"),
    (10173, 222, "galarian", "galarian corsola"),
    (10174, 263, "galarian", "galarian zigzagoon"),
    (10175, 264, "galarian", "galarian linoone"),
    (10176, 554, "galarian", "galarian darumaka"),
    (10177, 555, "galarian", "galarian darmanitan"),
    (10179, 562, "galarian", "galarian yamask"),
    (10180, 618, "galarian", "galarian stunfisk"),
    (10229, 58, "hisuian", "hisuian growlithe"),
    (10230, 59, "hisuian", "hisuian arcanine"),
    (10231, 100, "hisuian", "hisuian voltorb"),
    (10232, 101, "hisuian", "hisuian electrode"),
    (10233, 157, "hisuian", "hisuian typhlosion"),
    (10234, 211, "hisuian", "hisuian qwilfish"),
    (10235, 215, "hisuian", "hisuian sneasel"),
    (10236, 503, "hisuian", "hisuian samurott"),
    (10237, 549, "hisuian", "hisuian lilligant"),
    (10238, 570, "hisuian", "hisuian zorua"),
    (10239, 571, "hisuian", "hisuian zoroark"),
    (10240, 628, "hisuian", "hisuian braviary"),
    (10241, 705, "hisuian", "hisuian sliggoo"),
    (10242, 706, "hisuian", "hisuian goodra"),
    (10243, 713, "hisuian", "hisuian avalugg"),
    (10244, 724, "hisuian", "hisuian decidueye"),
    (10250, 128, "paldean", "paldean tauros (combat)"),
    (10251, 128, "paldean", "paldean tauros (blaze)"),
    (10252, 128, "paldean", "paldean tauros (aqua)"),
    (10253, 194, "paldean", "paldean wooper"),
)

# dex -> RegionalForm for every regional row.
FORMS_BY_DEX: dict[int, RegionalForm] = {
    dex: RegionalForm(dex=dex, base_dex=base, region=region, name_lower=name)
    for dex, base, region, name in _FORM_ROWS
}

# base dex -> its regional variants (Meowth and Tauros have more than one).
FORMS_BY_BASE: dict[int, tuple[RegionalForm, ...]] = {}
for _form in FORMS_BY_DEX.values():
    FORMS_BY_BASE[_form.base_dex] = FORMS_BY_BASE.get(_form.base_dex, ()) + (_form,)

# Every dex number that is a regional form, for cheap membership tests.
REGIONAL_DEX_IDS: frozenset[int] = frozenset(FORMS_BY_DEX)

# Base species that have at least one regional variant.
REGIONAL_BASE_DEX_IDS: frozenset[int] = frozenset(FORMS_BY_BASE)


# Regional evolution lines.  data/evolutions.json is generated from base-species
# chains only, so without this table a caught Galarian Meowth would either be
# unable to evolve or would evolve into base Persian.  Every target already
# exists in pokemon_species below dex 10000.
#
# form dex -> one or more (target dex, trigger, requirement)
#   trigger "level": requirement is the minimum level
#   trigger "item":  requirement is the item name as it appears in core.items
#   trigger "trade": requirement is None
REGIONAL_EVOLUTIONS: dict[int, tuple[tuple[int, str, object | None], ...]] = {
    10091: ((10092, "level", 20),),      # Alolan Rattata -> Alolan Raticate
    10101: ((10102, "item", "ice stone"),),   # Alolan Sandshrew -> Alolan Sandslash
    10103: ((10104, "item", "ice stone"),),   # Alolan Vulpix -> Alolan Ninetales
    10105: ((10106, "level", 26),),       # Alolan Diglett -> Alolan Dugtrio
    10107: ((10108, "level", 28),),       # Alolan Meowth -> Alolan Persian
    10109: ((10110, "level", 25),),       # Alolan Geodude -> Alolan Graveler
    10110: ((10111, "trade", None),),     # Alolan Graveler -> Alolan Golem
    10112: ((10113, "level", 38),),       # Alolan Grimer -> Alolan Muk
    10161: ((863, "level", 28),),         # Galarian Meowth -> Perrserker
    10162: ((10163, "level", 40),),       # Galarian Ponyta -> Galarian Rapidash
    10164: (                              # Galarian Slowpoke branches on item
        (10165, "item", "galarica cuff"),      # -> Galarian Slowbro
        (10172, "item", "galarica wreath"),    # -> Galarian Slowking
    ),
    10166: ((865, "level", 30),),         # Galarian Farfetch'd -> Sirfetch'd
    10173: ((864, "level", 38),),         # Galarian Corsola -> Cursola
    10174: ((10175, "level", 20),),       # Galarian Zigzagoon -> Galarian Linoone
    10175: ((862, "level", 35),),         # Galarian Linoone -> Obstagoon
    10176: ((10177, "level", 35),),       # Galarian Darumaka -> Galarian Darmanitan
    10179: ((867, "level", 34),),         # Galarian Yamask -> Runerigus
    10229: ((10230, "item", "fire stone"),),   # Hisuian Growlithe -> Hisuian Arcanine
    10231: ((10232, "item", "leaf stone"),),   # Hisuian Voltorb -> Hisuian Electrode
    10234: ((904, "level", 30),),         # Hisuian Qwilfish -> Overqwil
    10235: ((903, "item", "razor claw"),),     # Hisuian Sneasel -> Sneasler
    10238: ((10239, "level", 30),),       # Hisuian Zorua -> Hisuian Zoroark
    10241: ((10242, "level", 50),),       # Hisuian Sliggoo -> Hisuian Goodra
    10253: ((980, "level", 20),),         # Paldean Wooper -> Clodsire
}


def get_form(dex: int) -> RegionalForm | None:
    """Return the regional form for ``dex``, or None if it is not one."""
    return FORMS_BY_DEX.get(dex)


def is_regional(dex: int) -> bool:
    """Whether ``dex`` is a regional variant row."""
    return dex in REGIONAL_DEX_IDS


def get_base_dex(dex: int) -> int:
    """Map a regional dex to its base species; pass other dex numbers through.

    Used wherever a lookup keyed on the base species is needed -- learnsets,
    evolution chains, breeding -- because no ``pokemon_learnsets`` row exists
    for any dex >= 10000.
    """
    form = FORMS_BY_DEX.get(dex)
    return form.base_dex if form else dex


def get_forms_for_base(base_dex: int) -> tuple[RegionalForm, ...]:
    """Return every regional variant of ``base_dex`` (empty if it has none)."""
    return FORMS_BY_BASE.get(base_dex, ())


def get_evolution(dex: int) -> tuple[tuple[int, str, object | None], ...]:
    """Return every ``(target_dex, trigger, requirement)`` for a regional form."""
    return REGIONAL_EVOLUTIONS.get(dex, ())


# (base_dex, target_dex) pairs that a BASE species must not use.  Several
# evolutions in data/evolutions.json are only legal from the regional form --
# base Farfetch'd cannot become Sirfetch'd, only Galarian Farfetch'd can -- but
# the generated data files them under the base species' chain, so base Corsola,
# Linoone, Yamask, Qwilfish, Sneasel and Wooper would all evolve down a regional
# line.  Derived from REGIONAL_EVOLUTIONS so the two can never disagree.
FORM_EXCLUSIVE_EVOLUTIONS: frozenset[tuple[int, int]] = frozenset(
    (FORMS_BY_DEX[form_dex].base_dex, target)
    for form_dex, routes in REGIONAL_EVOLUTIONS.items()
    for target, _, _ in routes
    if target < 10000
)


def is_form_exclusive_evolution(species_id: int, target_dex: int) -> bool:
    """Whether ``species_id -> target_dex`` requires a regional form."""
    return (species_id, target_dex) in FORM_EXCLUSIVE_EVOLUTIONS


def split_region_query(query: str) -> tuple[str | None, str]:
    """Split a user query into ``(canonical_region, remainder)``.

    ``"galarian ponyta"`` -> ``("galarian", "ponyta")``;
    ``"alola vulpix"``    -> ``("alolan", "vulpix")``;
    ``"pikachu"``         -> ``(None, "pikachu")``.
    """
    parts = query.strip().lower().split(None, 1)
    if len(parts) == 2:
        region = REGION_ALIASES.get(parts[0])
        if region:
            return region, parts[1].strip()
    return None, query.strip().lower()


def find_form_dex(region: str, base_name: str) -> int | None:
    """Look up a form dex from a region plus a base name ("alolan", "vulpix")."""
    canonical = REGION_ALIASES.get(region.lower())
    if not canonical:
        return None
    target = base_name.strip().lower()
    for form in FORMS_BY_DEX.values():
        if form.region != canonical:
            continue
        short = form.short_name
        if short == target or short.replace(".", "").replace("'", "") == target.replace(
            ".", ""
        ).replace("'", ""):
            return form.dex
        # "paldean tauros" matches the first Tauros breed.
        if short.split(" (")[0] == target:
            return form.dex
    return None


async def verify_against_db(session) -> None:
    """Log any drift between this table and ``pokemon_species``.

    Called once at startup.  A mismatch means the species import changed form
    ids, in which case the spawn substitution would silently target rows that
    no longer exist -- so it is worth a loud warning rather than a KeyError
    later, mid-spawn.
    """
    from sqlalchemy import select

    from telemon.database.models import PokemonSpecies

    result = await session.execute(
        select(PokemonSpecies.national_dex, PokemonSpecies.name_lower).where(
            PokemonSpecies.national_dex >= 10000
        )
    )
    db_rows = {dex: name for dex, name in result.all()}

    missing = []
    renamed = []
    for form in FORMS_BY_DEX.values():
        db_name = db_rows.get(form.dex)
        if db_name is None:
            missing.append(form.dex)
        elif db_name != form.name_lower:
            renamed.append((form.dex, form.name_lower, db_name))

    unmapped = [
        dex
        for dex, name in db_rows.items()
        if dex not in FORMS_BY_DEX and not name.startswith("mega ")
    ]

    if missing or renamed or unmapped:
        logger.warning(
            "Regional form table drifted from pokemon_species",
            missing=missing,
            renamed=renamed,
            unmapped=unmapped,
        )
    else:
        logger.info("Regional form table verified", forms=len(FORMS_BY_DEX))
