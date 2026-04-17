"""Pokemon sprite emoji helper — loads emoji_map.json and provides inline emoji tags.

Supports two modes based on the bot ID detected at startup:
  - Premium mode (bot ID 8756134860): Returns <tg-emoji> HTML tags for inline
    custom emoji sprites.  Requires the bot owner to have Telegram Premium.
  - Plain mode   (bot ID 7939028029 or any other): Returns the fallback string
    (usually empty), so messages render without custom emoji.

Usage:
    # At startup (called from main.py after bot.get_me()):
    from telemon.core.emoji import init_emoji
    init_emoji(bot_info.id)

    # Anywhere in handlers:
    from telemon.core.emoji import poke_emoji, type_emoji
    text = f"{poke_emoji(25)} Pikachu"
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------
_EMOJI_MAP: dict[str, str] = {}
_LOADED = False
_PREMIUM_MODE = False

# Bot IDs
_PREMIUM_BOT_ID = 8756134860
_PLAIN_BOT_ID = 7939028029

# ---------------------------------------------------------------------------
# Type emoji mappings (Unicode fallback + custom emoji IDs if available)
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
# Loading
# ---------------------------------------------------------------------------
def _load_map() -> None:
    global _EMOJI_MAP, _LOADED
    if _LOADED:
        return
    map_path = Path(__file__).parent.parent.parent.parent / "data" / "emoji_map.json"
    if map_path.exists():
        try:
            _EMOJI_MAP = json.loads(map_path.read_text())
        except Exception:
            _EMOJI_MAP = {}
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
    global _PREMIUM_MODE
    _PREMIUM_MODE = bot_id == _PREMIUM_BOT_ID
    _load_map()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def reload_emoji_map() -> int:
    """Force reload the emoji map. Returns count of loaded emoji."""
    global _LOADED
    _LOADED = False
    _load_map()
    return len(_EMOJI_MAP)


def is_premium() -> bool:
    """Return whether the bot is running in premium (custom emoji) mode."""
    return _PREMIUM_MODE


def mode_label() -> str:
    """Human-readable mode label for startup logging."""
    return "✨ Premium" if _PREMIUM_MODE else "Standard"


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
    _load_map()
    eid = _EMOJI_MAP.get(str(dex_number))
    if not eid:
        return fallback
    return f'<tg-emoji emoji-id="{eid}">🔴</tg-emoji>'


def type_emoji(type_name: str) -> str:
    """Get an emoji badge for a Pokemon type.

    Returns the standard Unicode emoji for the type.  Works in both modes.

    Args:
        type_name: Type name (e.g. "fire", "water").

    Returns:
        Emoji string.
    """
    return _TYPE_UNICODE.get(type_name.lower(), "")


def rarity_emoji(species) -> str:
    """Get a rarity emoji string from a PokemonSpecies object.

    Works in both modes (uses Unicode emoji).

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


def has_emoji(dex_number: int) -> bool:
    """Check if we have a custom emoji for this species."""
    _load_map()
    return str(dex_number) in _EMOJI_MAP


def emoji_count() -> int:
    """Return how many emoji are loaded."""
    _load_map()
    return len(_EMOJI_MAP)
