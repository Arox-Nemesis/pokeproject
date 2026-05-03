"""Pokemon sprite emoji helper — loads emoji maps and provides inline emoji tags.

Supports two modes based on the bot ID detected at startup:
  - Premium mode (bot ID 8756134860): Returns <tg-emoji> HTML tags for inline
    custom emoji sprites.  Requires the bot owner to have Telegram Premium.
  - Plain mode   (bot ID 7939028029 or any other): Returns the fallback string
    (usually empty), so messages render without custom emoji.

Maps loaded (per-bot):
  - data/emoji_map_{bot_id}.json       — dex_number → custom_emoji_id  (Pokemon)
  - data/item_emoji_map_{bot_id}.json  — item_slug  → custom_emoji_id  (Items)
  - data/type_emoji_map_{bot_id}.json  — type_name  → custom_emoji_id  (Types)

Usage:
    # At startup (called from main.py after bot.get_me()):
    from telemon.core.emoji import init_emoji
    init_emoji(bot_info.id)

    # Anywhere in handlers:
    from telemon.core.emoji import poke_emoji, type_emoji, item_emoji
    text = f"{poke_emoji(25)} Pikachu"
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------
_EMOJI_MAP: dict[str, str] = {}
_ITEM_MAP: dict[str, str] = {}
_TYPE_MAP: dict[str, str] = {}
_FORM_MAP: dict[str, str] = {}
_STONE_MAP: dict[str, str] = {}
_LOADED = False
_PREMIUM_MODE = False
_BOT_ID: int | None = None

# Bot IDs
_PREMIUM_BOT_ID = 8756134860
_PLAIN_BOT_ID = 7939028029

# Data directory
_DATA_DIR = Path(__file__).parent.parent.parent.parent / "data"

# ---------------------------------------------------------------------------
# Type emoji mappings (Unicode fallbacks — always available)
# ---------------------------------------------------------------------------
_TYPE_UNICODE: dict[str, str] = {
    "normal": "⚪",
    "fire": "🔥",
    "water": "💧",
    "electric": "⚡",
    "grass": "🌿",
    "ice": "❄️",
    "fighting": "🥊",
    "poison": "☠️",
    "ground": "🏜️",
    "flying": "🕊️",
    "psychic": "🔮",
    "bug": "🐛",
    "rock": "🪨",
    "ghost": "👻",
    "dragon": "🐉",
    "dark": "🌑",
    "steel": "⚙️",
    "fairy": "🧚",
}

# ---------------------------------------------------------------------------
# Item emoji mappings (Unicode fallbacks)
# ---------------------------------------------------------------------------
_ITEM_UNICODE: dict[str, str] = {
    "fire-stone": "🔥",
    "water-stone": "💧",
    "thunder-stone": "⚡",
    "leaf-stone": "🌿",
    "moon-stone": "🌙",
    "sun-stone": "☀️",
    "dusk-stone": "🌑",
    "dawn-stone": "🌅",
    "shiny-stone": "✨",
    "ice-stone": "❄️",
    "rare-candy": "🍬",
    "lucky-egg": "🥚",
    "soothe-bell": "🔔",
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _load_json(filename: str) -> dict:
    """Load a JSON map file, trying per-bot name first, then generic fallback."""
    if _BOT_ID is not None:
        bot_path = _DATA_DIR / filename.replace(".json", f"_{_BOT_ID}.json")
        if bot_path.exists():
            try:
                return json.loads(bot_path.read_text())
            except Exception:
                pass
    # Fallback to generic name (backwards compatibility)
    path = _DATA_DIR / filename
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _load_maps() -> None:
    global _EMOJI_MAP, _ITEM_MAP, _TYPE_MAP, _FORM_MAP, _STONE_MAP, _LOADED
    if _LOADED:
        return

    _EMOJI_MAP = _load_json("emoji_map.json")
    _ITEM_MAP = _load_json("item_emoji_map.json")
    _TYPE_MAP = _load_json("type_emoji_map.json")
    _FORM_MAP = _load_json("form_emoji_map.json")
    _STONE_MAP = _load_json("stone_emoji_map.json")
    # Merge stones into item map so item_emoji() finds them without handler changes
    _ITEM_MAP.update(_STONE_MAP)
    _LOADED = True


# ---------------------------------------------------------------------------
# Initialization (called once at bot startup)
# ---------------------------------------------------------------------------
def init_emoji(bot_id: int) -> None:
    """Detect bot identity and set emoji mode.

    Call this once in main.py after ``bot.get_me()``.

    Args:
        bot_id: The Telegram bot user ID.
    """
    global _PREMIUM_MODE, _BOT_ID, _LOADED
    _BOT_ID = bot_id
    _PREMIUM_MODE = bot_id == _PREMIUM_BOT_ID
    _LOADED = False  # Force reload with new bot_id
    _load_maps()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def reload_all_maps() -> dict[str, int]:
    """Force reload all emoji maps. Returns counts per category."""
    global _LOADED
    _LOADED = False
    _load_maps()
    return {
        "pokemon": len(_EMOJI_MAP),
        "forms": len(_FORM_MAP),
        "items": len(_ITEM_MAP),
        "stones": len(_STONE_MAP),
        "types": len(_TYPE_MAP),
    }


def is_premium() -> bool:
    """Return whether the bot is running in premium (custom emoji) mode."""
    return _PREMIUM_MODE


def mode_label() -> str:
    """Human-readable mode label for startup logging."""
    return "✨ Premium" if _PREMIUM_MODE else "Standard"


def emoji_count() -> int:
    """Return how many Pokemon emoji are loaded."""
    _load_maps()
    return len(_EMOJI_MAP)


def total_emoji_count() -> dict[str, int]:
    """Return counts for all emoji categories."""
    _load_maps()
    return {
        "pokemon": len(_EMOJI_MAP),
        "forms": len(_FORM_MAP),
        "items": len(_ITEM_MAP),
        "stones": len(_STONE_MAP),
        "types": len(_TYPE_MAP),
    }


def _tg_emoji_tag(emoji_id: str, placeholder: str = "🔴") -> str:
    """Build a <tg-emoji> HTML tag."""
    return f'<tg-emoji emoji-id="{emoji_id}">{placeholder}</tg-emoji>'


# ---------------------------------------------------------------------------
# Pokemon emoji
# ---------------------------------------------------------------------------
def poke_emoji(dex_number: int, fallback: str = "") -> str:
    """Get an inline custom emoji tag for a Pokemon species.

    In premium mode, returns an HTML ``<tg-emoji>`` tag that Telegram renders
    as the uploaded sprite sticker.  In plain mode (or if the species has no
    mapped emoji), returns *fallback* (default empty string).

    Args:
        dex_number: National Pokedex number (1-1025).
        fallback: String to return when custom emoji is unavailable.

    Returns:
        HTML string or fallback.
    """
    if not _PREMIUM_MODE:
        return fallback
    _load_maps()
    eid = _EMOJI_MAP.get(str(dex_number))
    if not eid:
        return fallback
    return _tg_emoji_tag(eid)


def has_emoji(dex_number: int) -> bool:
    """Check if we have a custom emoji for this species."""
    _load_maps()
    return str(dex_number) in _EMOJI_MAP


# ---------------------------------------------------------------------------
# Form emoji (non-numeric stems like Unown letters, Vivillon patterns)
# ---------------------------------------------------------------------------
def form_emoji(stem: str, fallback: str = "") -> str:
    """Get an inline custom emoji tag for a non-numeric Pokemon form.

    Args:
        stem: Form stem (e.g. "pikachu-cosplay", "unown-a").
        fallback: String to return when custom emoji is unavailable.

    Returns:
        HTML string or fallback.
    """
    if not _PREMIUM_MODE:
        return fallback
    _load_maps()
    eid = _FORM_MAP.get(stem)
    if not eid:
        return fallback
    return _tg_emoji_tag(eid)


# ---------------------------------------------------------------------------
# Type emoji
# ---------------------------------------------------------------------------
def type_emoji(type_name: str) -> str:
    """Get an emoji badge for a Pokemon type.

    In premium mode with type emoji uploaded, returns a custom emoji tag.
    Otherwise returns the standard Unicode emoji for the type.

    Args:
        type_name: Type name (e.g. "fire", "water").

    Returns:
        Emoji string (custom or Unicode).
    """
    lower = type_name.lower()
    if _PREMIUM_MODE:
        _load_maps()
        eid = _TYPE_MAP.get(lower)
        if eid:
            fallback = _TYPE_UNICODE.get(lower, "")
            return _tg_emoji_tag(eid, fallback or "⚪")
    return _TYPE_UNICODE.get(lower, "")


# ---------------------------------------------------------------------------
# Item emoji
# ---------------------------------------------------------------------------
def item_emoji(item_name: str, fallback: str = "") -> str:
    """Get an inline custom emoji tag for an item.

    Args:
        item_name: Item name (e.g. "Fire Stone") or slug (e.g. "fire-stone").
        fallback: String to return when custom emoji is unavailable.

    Returns:
        HTML string, Unicode fallback, or empty string.
    """
    # Normalize to slug format
    slug = item_name.lower().replace("'", "").replace(" ", "-")

    if _PREMIUM_MODE:
        _load_maps()
        eid = _ITEM_MAP.get(slug)
        if eid:
            placeholder = _ITEM_UNICODE.get(slug, "🎒")
            return _tg_emoji_tag(eid, placeholder)

    # Unicode fallback
    return _ITEM_UNICODE.get(slug, fallback)


# ---------------------------------------------------------------------------
# Rarity emoji (works in both modes — Unicode only)
# ---------------------------------------------------------------------------
def rarity_emoji(species) -> str:
    """Get a rarity emoji string from a PokemonSpecies object.

    Args:
        species: A PokemonSpecies-like object with ``is_mythical``,
                 ``is_legendary``, and ``catch_rate`` attributes.

    Returns:
        Rarity label with emoji, or empty string for common species.
    """
    if species.is_mythical:
        return "🌟 <b>MYTHICAL</b>"
    if species.is_legendary:
        return "⭐ <b>LEGENDARY</b>"
    if species.catch_rate <= 3:
        return "💎 <b>Ultra Rare</b>"
    if species.catch_rate <= 45:
        return "🔷 <b>Rare</b>"
    if species.catch_rate <= 120:
        return "🔹 Uncommon"
    return ""
