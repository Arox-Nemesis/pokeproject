"""Upload Pokemon, item, and type sprites as Telegram custom emoji.

Creates category-based sticker sets and saves ID mappings to JSON files.
Set names include a version tag (SET_VERSION) to avoid Telegram cache conflicts.

Pokemon sets (all write to emoji_map_{bot_id}.json):
  telemon_{ver}_base_{N}_by_{user}     — Base Pokemon dex 1-1025 (6 sets, 200/set)
  telemon_{ver}_mega_by_{user}         — Mega evolutions (~50 sprites)
  telemon_{ver}_regional_by_{user}     — Regional forms (57 sprites)
  telemon_{ver}_forms_{N}_by_{user}    — Other alt forms, numeric IDs (10000+)

Form set (writes to form_emoji_map_{bot_id}.json):
  telemon_{ver}_cosmetic_{N}_by_{user} — Non-numeric form variants (Unown, Vivillon, etc.)

Item/stone/type sets:
  telemon_{ver}_items_by_{user}        — Non-stone items (item_emoji_map_{bot_id}.json)
  telemon_{ver}_stones_by_{user}       — Evo + mega stones (stone_emoji_map_{bot_id}.json)
  telemon_{ver}_types_by_{user}        — Type icons (type_emoji_map_{bot_id}.json)

Commands:
  base       Upload base Pokemon (1-1025)
  megas      Upload mega evolutions
  regionals  Upload regional forms
  forms      Upload other alt forms (numeric + cosmetic)
  items      Upload non-stone items
  stones     Upload evo + mega stones
  types      Upload type icons
  all        Upload everything
  remap      Re-read existing sets and rebuild all maps
  validate   Check all mapped emoji still exist on Telegram
  delete-old Delete ALL sticker sets (legacy + current) & clear maps
  status     Show current map statistics
  --dry-run  Test sprite loading only (combine with any upload command)
  --fix      With validate: remove dead entries from maps
"""

import asyncio
import csv
import io
import json
import re
import struct
import sys
import time
from pathlib import Path

import aiohttp
from PIL import Image

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Configuration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
CSV_DIR = DATA_DIR / "csv"
POKEMON_SPRITE_DIR = DATA_DIR / "sprites" / "pokemon"
ITEM_SPRITE_DIR = DATA_DIR / "sprites" / "items"
TYPE_SPRITE_DIR = DATA_DIR / "sprites" / "types" / "generation-ix" / "scarlet-violet"
SPRITE_REMOTE = (
    "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/{pid}.png"
)

OWNER_ID = 6894738352
MAX_PER_SET = 200
SPRITE_SIZE = 100
SET_VERSION = "v2"
_LEGACY_USERNAMES = ["TelemonXRobot"]

_API_CONCURRENCY = 3
_API_PACE = 0.12
_bots: list[dict] = []
_api_sem: asyncio.Semaphore | None = None
_throttle_lock: asyncio.Lock | None = None
_last_call_time: float = 0.0

# Canonical mega evolution IDs (from scripts/fetch_megas.py)
CANONICAL_MEGA_IDS: set[int] = {
    10033, 10034, 10035, 10036, 10037, 10038, 10039, 10040,
    10041, 10042, 10043, 10044, 10045, 10046, 10047, 10048,
    10049, 10050, 10051, 10052, 10053, 10054, 10055, 10056,
    10057, 10058, 10059, 10060, 10062, 10063, 10064, 10065,
    10066, 10067, 10068, 10069, 10070, 10071, 10072, 10073,
    10074, 10075, 10076, 10079, 10087, 10088, 10089, 10090,
}

# Regional form IDs (from scripts/fetch_regional.py)
REGIONAL_IDS: set[int] = {
    10091, 10092, 10100, 10101, 10102, 10103, 10104, 10105,
    10106, 10107, 10108, 10109, 10110, 10111, 10112, 10113,
    10114, 10115,
    10161, 10162, 10163, 10164, 10165, 10166, 10167, 10168,
    10169, 10170, 10171, 10172, 10173, 10174, 10175, 10176,
    10177, 10179, 10180,
    10229, 10230, 10231, 10232, 10233, 10234, 10235, 10236,
    10237, 10238, 10239, 10240, 10241, 10242, 10243, 10244,
    10250, 10251, 10252, 10253,
}

EVO_STONE_SLUGS: set[str] = {
    "fire-stone", "water-stone", "thunder-stone", "leaf-stone",
    "moon-stone", "sun-stone", "dusk-stone", "dawn-stone",
    "shiny-stone", "ice-stone",
}

MEGA_STONE_SLUGS: set[str] = {
    "venusaurite", "charizardite-x", "charizardite-y", "blastoisinite",
    "beedrillite", "pidgeotite", "alakazite", "slowbronite",
    "gengarite", "kangaskhanite", "pinsirite", "gyaradosite",
    "aerodactylite", "mewtwonite-x", "mewtwonite-y", "ampharosite",
    "steelixite", "scizorite", "heracronite", "houndoominite",
    "tyranitarite", "sceptilite", "blazikenite", "swampertite",
    "gardevoirite", "sablenite", "mawilite", "aggronite",
    "medichamite", "manectite", "sharpedonite", "cameruptite",
    "altarianite", "banettite", "absolite", "glalitite",
    "salamencite", "metagrossite", "latiasite", "latiosite",
    "lopunnite", "garchompite", "lucarionite", "abomasite",
    "galladite", "audinite", "diancite",
}

ALL_STONE_SLUGS = EVO_STONE_SLUGS | MEGA_STONE_SLUGS

_SPRITE_EXCLUDE = {"0", "egg", "egg-manaphy", "substitute"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Map file paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def pokemon_map_path(bot_id: int) -> Path:
    return DATA_DIR / f"emoji_map_{bot_id}.json"

def form_map_path(bot_id: int) -> Path:
    return DATA_DIR / f"form_emoji_map_{bot_id}.json"

def item_map_path(bot_id: int) -> Path:
    return DATA_DIR / f"item_emoji_map_{bot_id}.json"

def stone_map_path(bot_id: int) -> Path:
    return DATA_DIR / f"stone_emoji_map_{bot_id}.json"

def type_map_path(bot_id: int) -> Path:
    return DATA_DIR / f"type_emoji_map_{bot_id}.json"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CSV loading & discovery
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_pokemon_csv_cache: list[dict] | None = None


def _load_pokemon_csv() -> list[dict]:
    global _pokemon_csv_cache
    if _pokemon_csv_cache is None:
        with open(CSV_DIR / "pokemon.csv") as f:
            _pokemon_csv_cache = [
                {
                    "id": int(r["id"]),
                    "identifier": r["identifier"],
                    "species_id": int(r["species_id"]),
                    "is_default": int(r.get("is_default") or 0),
                }
                for r in csv.DictReader(f)
            ]
    return _pokemon_csv_cache


def _all_mega_ids() -> set[int]:
    rows = _load_pokemon_csv()
    custom = {
        r["id"] for r in rows
        if r["id"] >= 10278 and "-mega" in r["identifier"]
    }
    return CANONICAL_MEGA_IDS | custom


def discover_base() -> list[int]:
    """Base Pokemon IDs (1-1025) with sprites."""
    rows = _load_pokemon_csv()
    ids = sorted(
        r["id"] for r in rows
        if r["is_default"] == 1 and r["species_id"] <= 1025
    )
    return [pid for pid in ids if (POKEMON_SPRITE_DIR / f"{pid}.png").exists()]


def discover_megas() -> list[int]:
    """All mega Pokemon IDs with sprites."""
    return sorted(
        pid for pid in _all_mega_ids()
        if (POKEMON_SPRITE_DIR / f"{pid}.png").exists()
    )


def discover_regionals() -> list[int]:
    """Regional form IDs with sprites."""
    return sorted(
        pid for pid in REGIONAL_IDS
        if (POKEMON_SPRITE_DIR / f"{pid}.png").exists()
    )


def discover_numeric_forms() -> list[int]:
    """Numeric alt-form IDs (10000+), excluding megas and regionals, with sprites."""
    megas = _all_mega_ids()
    form_ids = []
    if POKEMON_SPRITE_DIR.exists():
        for p in POKEMON_SPRITE_DIR.iterdir():
            if p.suffix == ".png" and p.stem.isdigit():
                pid = int(p.stem)
                if pid >= 10000 and pid not in megas and pid not in REGIONAL_IDS:
                    form_ids.append(pid)
    return sorted(form_ids)


def discover_cosmetic_forms() -> list[str]:
    """Non-numeric sprite stems (Unown letters, Vivillon patterns, Alcremie, etc.)."""
    stems = []
    if POKEMON_SPRITE_DIR.exists():
        for p in POKEMON_SPRITE_DIR.iterdir():
            if (
                p.suffix == ".png"
                and p.is_file()
                and not p.stem.isdigit()
                and p.stem not in _SPRITE_EXCLUDE
            ):
                stems.append(p.stem)
    return sorted(stems)


def _parse_bot_items() -> list[dict]:
    """Parse items from core/items.py."""
    items_py = Path(__file__).parent.parent / "core" / "items.py"
    if not items_py.exists():
        return []
    content = items_py.read_text()
    names = re.findall(r'"name":\s*"([^"]+)"', content)
    ids = re.findall(r'"id":\s*(\d+)', content)
    result = []
    for i, name in enumerate(names):
        slug = name.lower().replace("'", "").replace(" ", "-")
        item_id = int(ids[i]) if i < len(ids) else i + 1
        result.append({"name": name, "slug": slug, "id": item_id})
    return result


def discover_items() -> list[str]:
    """Non-stone item slugs with sprites."""
    return sorted(
        it["slug"] for it in _parse_bot_items()
        if it["slug"] not in ALL_STONE_SLUGS
        and (ITEM_SPRITE_DIR / f"{it['slug']}.png").exists()
    )


def discover_stones() -> list[str]:
    """Stone slugs (evo + mega) with sprites."""
    return sorted(
        it["slug"] for it in _parse_bot_items()
        if it["slug"] in ALL_STONE_SLUGS
        and (ITEM_SPRITE_DIR / f"{it['slug']}.png").exists()
    )


def discover_types() -> list[tuple[int, str]]:
    """Type icons (IDs 1-18)."""
    path = CSV_DIR / "types.csv"
    if not path.exists():
        return []
    with open(path) as f:
        types = {int(r["id"]): r["identifier"] for r in csv.DictReader(f)}
    return sorted(
        (tid, name) for tid, name in types.items()
        if 1 <= tid <= 18
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Sprite loading
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _strip_bad_png_chunks(raw: bytes) -> bytes:
    """Strip zTXt/iCCP/iTXt chunks that cause Pillow to reject some PNGs."""
    if raw[:8] != b'\x89PNG\r\n\x1a\n':
        return raw
    skip = {b'zTXt', b'iCCP', b'iTXt'}
    clean = raw[:8]
    pos = 8
    while pos + 8 <= len(raw):
        length = struct.unpack('>I', raw[pos:pos+4])[0]
        ctype = raw[pos+4:pos+8]
        chunk_end = pos + 12 + length
        if chunk_end > len(raw):
            break
        if ctype not in skip:
            clean += raw[pos:chunk_end]
        pos = chunk_end
        if ctype == b'IEND':
            break
    return clean


def process_sprite(raw: bytes) -> bytes | None:
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        # Retry after stripping problematic metadata chunks
        try:
            img = Image.open(io.BytesIO(_strip_bad_png_chunks(raw))).convert("RGBA")
        except Exception as e:
            print(f"  [WARN] process_sprite: {e}")
            return None
    try:
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        ratio = min(SPRITE_SIZE / img.width, SPRITE_SIZE / img.height)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.NEAREST)
        final = Image.new("RGBA", (SPRITE_SIZE, SPRITE_SIZE), (0, 0, 0, 0))
        offset = (
            (SPRITE_SIZE - new_size[0]) // 2,
            (SPRITE_SIZE - new_size[1]) // 2,
        )
        final.paste(img, offset)
        buf = io.BytesIO()
        final.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        print(f"  [WARN] process_sprite: {e}")
        return None


def load_local_sprite(path: Path) -> bytes | None:
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        if len(raw) < 100:
            return None
        return process_sprite(raw)
    except Exception:
        return None


async def _fetch_remote(http: aiohttp.ClientSession, pid: int) -> bytes | None:
    url = SPRITE_REMOTE.format(pid=pid)
    for attempt in range(1, 6):
        try:
            async with http.get(url) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    raise OSError(f"HTTP {resp.status}")
                raw = await resp.read()
                if len(raw) < 100:
                    raise ValueError("Too small")
                result = process_sprite(raw)
                if result is not None:
                    return result
                print(f"  [SKIP] Sprite {pid}: unsupported format")
                return None
        except (OSError, ValueError, aiohttp.ClientError) as e:
            if attempt < 5:
                wait = min(2 ** attempt, 30)
                print(f"  [WARN] Sprite {pid} attempt {attempt}: {e} — retry in {wait}s")
                await asyncio.sleep(wait)
    return None


async def _load_id_sprites(
    ids: list[int], label: str = "sprites"
) -> dict[int, bytes]:
    """Load sprites for numeric Pokemon IDs (local first, remote fallback)."""
    print(f"\n📥 Loading {len(ids)} {label}...\n")
    sprites: dict[int, bytes] = {}
    start = time.time()

    async with aiohttp.ClientSession() as http:
        for i, pid in enumerate(ids):
            data = load_local_sprite(POKEMON_SPRITE_DIR / f"{pid}.png")
            if not data:
                data = await _fetch_remote(http, pid)
            if data:
                sprites[pid] = data
            done = i + 1
            if done % 100 == 0 or done == len(ids):
                print(
                    f"  {progress_bar(done, len(ids))}  "
                    f"elapsed {elapsed_str(start)}  "
                    f"ETA {eta_str(start, done, len(ids))}"
                )

    failed = len(ids) - len(sprites)
    print(f"\n  ✅ Loaded: {len(sprites)}  |  ❌ Failed: {failed}")
    return sprites


def _load_slug_sprites(
    slugs: list[str], sprite_dir: Path, label: str = "sprites"
) -> dict[str, bytes]:
    """Load sprites for slug-based entries (items, stones, types)."""
    print(f"\n📥 Loading {len(slugs)} {label}...")
    sprites: dict[str, bytes] = {}
    for slug in slugs:
        data = load_local_sprite(sprite_dir / f"{slug}.png")
        if data:
            sprites[slug] = data
    failed = len(slugs) - len(sprites)
    print(f"  ✅ Loaded: {len(sprites)}  |  ❌ Failed: {failed}")
    return sprites


async def _load_stem_sprites(
    stems: list[str], label: str = "sprites"
) -> dict[str, bytes]:
    """Load sprites for non-numeric stems (local only, remote for numeric part)."""
    print(f"\n📥 Loading {len(stems)} {label}...\n")
    sprites: dict[str, bytes] = {}
    start = time.time()

    async with aiohttp.ClientSession() as http:
        for i, stem in enumerate(stems):
            data = load_local_sprite(POKEMON_SPRITE_DIR / f"{stem}.png")
            if not data and stem.isdigit():
                data = await _fetch_remote(http, int(stem))
            if data:
                sprites[stem] = data
            done = i + 1
            if done % 100 == 0 or done == len(stems):
                print(
                    f"  {progress_bar(done, len(stems))}  "
                    f"elapsed {elapsed_str(start)}  "
                    f"ETA {eta_str(start, done, len(stems))}"
                )

    failed = len(stems) - len(sprites)
    print(f"\n  ✅ Loaded: {len(sprites)}  |  ❌ Failed: {failed}")
    return sprites


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Display helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def progress_bar(current: int, total: int, width: int = 30) -> str:
    filled = int(current / total * width) if total else 0
    bar = "█" * filled + "░" * (width - filled)
    pct = current / total * 100 if total else 0
    return f"[{bar}] {pct:5.1f}%  ({current}/{total})"


def elapsed_str(start: float) -> str:
    m, s = divmod(int(time.time() - start), 60)
    return f"{m:02d}:{s:02d}"


def eta_str(start: float, done: int, total: int) -> str:
    if done == 0:
        return "--:--"
    remaining = (total - done) / (done / (time.time() - start))
    m, s = divmod(int(remaining), 60)
    return f"~{m:02d}:{s:02d}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Bot init & Telegram API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _get_bot_tokens() -> list[str]:
    env_path = DATA_DIR.parent / ".env"
    tokens: list[str] = []
    seen: set[str] = set()
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "BOT_TOKEN" in line.split("=", 1)[0]:
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            if val and val not in seen:
                tokens.append(val)
                seen.add(val)
    if not tokens:
        raise RuntimeError("No BOT_TOKEN found in .env")
    return tokens


async def _init_bots(bot_filter: str | None = None) -> None:
    """Discover bots from .env and optionally filter to one.

    Args:
        bot_filter: "1" or "2" for bot by position, or a numeric bot ID,
                    or None/\"both\" for all bots.
    """
    global _bots, _api_sem, _throttle_lock
    tokens = _get_bot_tokens()
    all_bots = []
    async with aiohttp.ClientSession() as http:
        for token in tokens:
            resp = await http.get(f"https://api.telegram.org/bot{token}/getMe")
            result = await resp.json()
            if result.get("ok"):
                info = result["result"]
                all_bots.append({
                    "token": token,
                    "username": info["username"],
                    "bot_id": info["id"],
                })
                print(f"  Bot: @{info['username']} (ID {info['id']})")
            else:
                print(f"  [WARN] getMe failed: {result.get('description')}")
    if not all_bots:
        raise RuntimeError("No valid bot tokens")

    if bot_filter and bot_filter != "both":
        matched = None
        if bot_filter in ("1", "2"):
            idx = int(bot_filter) - 1
            if idx < len(all_bots):
                matched = [all_bots[idx]]
            else:
                raise RuntimeError(f"Bot #{bot_filter} not found (only {len(all_bots)} bot(s) available)")
        else:
            matched = [b for b in all_bots if str(b["bot_id"]) == bot_filter]
            if not matched:
                ids = ", ".join(str(b["bot_id"]) for b in all_bots)
                raise RuntimeError(f"Bot ID {bot_filter} not found. Available: {ids}")
        _bots = matched
        print(f"  → Selected: @{_bots[0]['username']} (ID {_bots[0]['bot_id']})")
    else:
        _bots = all_bots
        print(f"  → Using all {len(_bots)} bot(s)")

    _api_sem = asyncio.Semaphore(_API_CONCURRENCY)
    _throttle_lock = asyncio.Lock()


async def _throttle() -> None:
    global _last_call_time
    assert _api_sem and _throttle_lock
    async with _throttle_lock:
        now = time.time()
        wait = _API_PACE - (now - _last_call_time)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_time = time.time()


async def tg_create_set(
    http: aiohttp.ClientSession,
    token: str,
    name: str,
    title: str,
    png: bytes,
    emoji: str = "🔴",
) -> bool:
    async with _api_sem:
        await _throttle()
        form = aiohttp.FormData()
        form.add_field("user_id", str(OWNER_ID))
        form.add_field("name", name)
        form.add_field("title", title)
        form.add_field("sticker_type", "custom_emoji")
        form.add_field("stickers", json.dumps([{
            "sticker": "attach://file0",
            "emoji_list": [emoji],
            "format": "static",
        }]))
        form.add_field("file0", png, filename="sprite.png", content_type="image/png")
        resp = await http.post(
            f"https://api.telegram.org/bot{token}/createNewStickerSet", data=form,
        )
        result = await resp.json()
    if not result.get("ok"):
        desc = result.get("description", "")
        if "Too Many Requests" in desc or result.get("error_code") == 429:
            wait = result.get("parameters", {}).get("retry_after", 5) + 1
            print(f"  ⏳ Rate limited — {wait}s...")
            await asyncio.sleep(wait)
            return await tg_create_set(http, token, name, title, png, emoji)
        print(f"  [ERR] createNewStickerSet: {desc}")
        return False
    return True


async def tg_add_sticker(
    http: aiohttp.ClientSession,
    token: str,
    name: str,
    png: bytes,
    emoji: str = "🔴",
) -> bool:
    async with _api_sem:
        await _throttle()
        form = aiohttp.FormData()
        form.add_field("user_id", str(OWNER_ID))
        form.add_field("name", name)
        form.add_field("sticker", json.dumps({
            "sticker": "attach://file0",
            "emoji_list": [emoji],
            "format": "static",
        }))
        form.add_field("file0", png, filename="sprite.png", content_type="image/png")
        resp = await http.post(
            f"https://api.telegram.org/bot{token}/addStickerToSet", data=form,
        )
        result = await resp.json()
    if not result.get("ok"):
        desc = result.get("description", "")
        if "Too Many Requests" in desc or result.get("error_code") == 429:
            wait = result.get("parameters", {}).get("retry_after", 5) + 1
            print(f"  ⏳ Rate limited — {wait}s...")
            await asyncio.sleep(wait)
            return await tg_add_sticker(http, token, name, png, emoji)
        print(f"  [ERR] addStickerToSet: {desc}")
        return False
    return True


async def tg_get_set(
    http: aiohttp.ClientSession, token: str, name: str,
) -> dict | None:
    resp = await http.get(
        f"https://api.telegram.org/bot{token}/getStickerSet",
        params={"name": name},
    )
    result = await resp.json()
    return result["result"] if result.get("ok") else None


async def tg_delete_set(
    http: aiohttp.ClientSession, token: str, name: str,
) -> bool:
    resp = await http.post(
        f"https://api.telegram.org/bot{token}/deleteStickerSet",
        json={"name": name},
    )
    result = await resp.json()
    return result.get("ok", False)


async def tg_check_emoji(
    http: aiohttp.ClientSession, token: str, emoji_ids: list[str],
) -> set[str]:
    valid: set[str] = set()
    for i in range(0, len(emoji_ids), 200):
        batch = emoji_ids[i : i + 200]
        resp = await http.post(
            f"https://api.telegram.org/bot{token}/getCustomEmojiStickers",
            json={"custom_emoji_ids": batch},
        )
        result = await resp.json()
        if result.get("ok"):
            for s in result["result"]:
                eid = s.get("custom_emoji_id", "")
                if eid:
                    valid.add(eid)
        await asyncio.sleep(0.15)
    return valid


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Map I/O
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _load_map(path: Path) -> dict[str, str]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _save_map(path: Path, data: dict[str, str]) -> None:
    path.write_text(json.dumps(data, indent=2))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Core upload engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _upload_to_sets(
    label: str,
    keys: list[str],
    sprites: dict[str, bytes],
    bot_token: str,
    username: str,
    emoji_map: dict[str, str],
    map_path: Path,
    set_name_fn,
    set_title_fn,
    base_emoji: str = "🔴",
) -> tuple[int, int]:
    """Upload sprites into Telegram sticker sets and update the map.

    Returns (done, fail) counts.
    """
    available = [k for k in keys if k in sprites]
    already_in_map = set(emoji_map.keys())
    remaining = [k for k in available if k not in already_in_map]

    print(f"  Already mapped: {len(set(available) & already_in_map)}")
    print(f"  Remaining: {len(remaining)}")

    if not remaining:
        print(f"  ✅ All {label} already mapped!")
        return 0, 0

    key_to_idx = {k: i for i, k in enumerate(available)}
    batches: dict[int, list[str]] = {}
    for k in remaining:
        batch_num = key_to_idx[k] // MAX_PER_SET + 1
        batches.setdefault(batch_num, []).append(k)

    upload_start = time.time()
    total_done, total_fail = 0, 0

    for batch_num in sorted(batches):
        batch_keys = batches[batch_num]
        sname = set_name_fn(batch_num, username)
        title = set_title_fn(batch_num)

        batch_start = (batch_num - 1) * MAX_PER_SET
        batch_end = batch_num * MAX_PER_SET
        full_batch = available[batch_start:batch_end]

        async with aiohttp.ClientSession() as http:
            set_data = await tg_get_set(http, bot_token, sname)
            set_exists = set_data is not None
            existing_count = len(set_data.get("stickers", [])) if set_data else 0

            if set_exists and existing_count >= len(full_batch):
                stickers = set_data["stickers"]
                mapped = 0
                for idx, s in enumerate(stickers):
                    if idx < len(full_batch):
                        eid = s.get("custom_emoji_id", "")
                        if eid:
                            emoji_map[full_batch[idx]] = eid
                            mapped += 1
                _save_map(map_path, emoji_map)
                total_done += len(batch_keys)
                print(f"  Set {batch_num}: full ({existing_count}) → remapped {mapped}")
                continue

            if set_exists:
                print(f"  Set {batch_num}: partial ({existing_count}/{len(full_batch)} stickers)")

            uploaded: list[str] = []
            for key in batch_keys:
                png = sprites[key]
                if not set_exists:
                    ok = await tg_create_set(http, bot_token, sname, title, png, base_emoji)
                    if ok:
                        set_exists = True
                        uploaded.append(key)
                        total_done += 1
                    else:
                        total_fail += 1
                else:
                    ok = await tg_add_sticker(http, bot_token, sname, png, base_emoji)
                    if ok:
                        uploaded.append(key)
                        total_done += 1
                    else:
                        total_fail += 1

                if total_done > 0 and total_done % 25 == 0:
                    print(
                        f"  {progress_bar(total_done, len(remaining))}  "
                        f"elapsed {elapsed_str(upload_start)}  "
                        f"ETA {eta_str(upload_start, total_done, len(remaining))}"
                    )

            set_data = await tg_get_set(http, bot_token, sname)
            if set_data:
                stickers = set_data["stickers"]
                for idx, s in enumerate(stickers):
                    if idx < len(full_batch):
                        eid = s.get("custom_emoji_id", "")
                        if eid:
                            emoji_map[full_batch[idx]] = eid

            _save_map(map_path, emoji_map)
            print(
                f"  Set {batch_num}: {len(uploaded)} uploaded, "
                f"{len(emoji_map)} total mapped"
            )

    return total_done, total_fail


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Category upload commands
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _print_header(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


async def _run_pokemon_upload(
    label: str,
    ids: list[int],
    set_name_fn,
    set_title_fn,
    dry_run: bool = False,
) -> None:
    """Common flow for Pokemon-category uploads (base/mega/regional/forms)."""
    _print_header(f"{label.upper()} EMOJI UPLOAD")
    print(f"  Entries: {len(ids)}")

    if not ids:
        print("  No entries found.")
        return

    if dry_run:
        print("\n📥 DRY RUN: Testing sample sprites...")
        async with aiohttp.ClientSession() as http:
            loaded = 0
            for pid in ids[:20]:
                data = load_local_sprite(POKEMON_SPRITE_DIR / f"{pid}.png")
                if not data:
                    data = await _fetch_remote(http, pid)
                if data:
                    loaded += 1
                    print(f"  ✅ #{pid} ({len(data)} bytes)")
                else:
                    print(f"  ❌ #{pid} — missing")
        print(f"\n  Loaded {loaded}/min({len(ids)},20) sample sprites")
        return

    sprites = await _load_id_sprites(ids, label)
    if not sprites:
        print("  No sprites to upload.")
        return

    str_sprites = {str(k): v for k, v in sprites.items()}
    available_keys = [str(pid) for pid in sorted(sprites.keys())]

    for bot in _bots:
        map_path = pokemon_map_path(bot["bot_id"])
        emoji_map = _load_map(map_path)

        print(f"\n📤 @{bot['username']} (ID {bot['bot_id']})")
        done, fail = await _upload_to_sets(
            label, available_keys, str_sprites,
            bot["token"], bot["username"],
            emoji_map, map_path,
            set_name_fn, set_title_fn,
        )
        print(f"  ✅ {done} done, {fail} failed → {map_path.name}")


async def upload_base(dry_run: bool = False) -> None:
    ids = discover_base()
    await _run_pokemon_upload(
        "base",
        ids,
        set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_base_{b}_by_{u}",
        set_title_fn=lambda b: f"Telemon Base #{(b-1)*200+1:04d}-{min(b*200, 1025):04d}",
        dry_run=dry_run,
    )


async def upload_megas(dry_run: bool = False) -> None:
    ids = discover_megas()
    await _run_pokemon_upload(
        "mega",
        ids,
        set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_mega_by_{u}" if b == 1 else f"telemon_{SET_VERSION}_mega_{b}_by_{u}",
        set_title_fn=lambda b: "Telemon Mega Evolutions",
        dry_run=dry_run,
    )


async def upload_regionals(dry_run: bool = False) -> None:
    ids = discover_regionals()
    await _run_pokemon_upload(
        "regional",
        ids,
        set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_regional_by_{u}" if b == 1 else f"telemon_{SET_VERSION}_regional_{b}_by_{u}",
        set_title_fn=lambda b: "Telemon Regional Forms",
        dry_run=dry_run,
    )


async def upload_numeric_forms(dry_run: bool = False) -> None:
    ids = discover_numeric_forms()
    await _run_pokemon_upload(
        "forms (numeric)",
        ids,
        set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_forms_{b}_by_{u}",
        set_title_fn=lambda b: f"Telemon Forms #{b}",
        dry_run=dry_run,
    )


async def upload_cosmetic_forms(dry_run: bool = False) -> None:
    _print_header("COSMETIC FORMS EMOJI UPLOAD")
    stems = discover_cosmetic_forms()
    print(f"  Entries: {len(stems)}")

    if not stems:
        print("  No entries found.")
        return

    if dry_run:
        print("\n📥 DRY RUN: Testing sample sprites...")
        loaded = 0
        for stem in stems[:20]:
            data = load_local_sprite(POKEMON_SPRITE_DIR / f"{stem}.png")
            if data:
                loaded += 1
                print(f"  ✅ {stem} ({len(data)} bytes)")
            else:
                print(f"  ❌ {stem} — missing")
        print(f"\n  Loaded {loaded}/min({len(stems)},20) sample sprites")
        return

    sprites = await _load_stem_sprites(stems, "cosmetic form sprites")
    if not sprites:
        print("  No sprites to upload.")
        return

    available_keys = sorted(sprites.keys())

    for bot in _bots:
        map_path = form_map_path(bot["bot_id"])
        emoji_map = _load_map(map_path)

        print(f"\n📤 @{bot['username']} (ID {bot['bot_id']})")
        done, fail = await _upload_to_sets(
            "cosmetic forms", available_keys, sprites,
            bot["token"], bot["username"],
            emoji_map, map_path,
            set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_cosmetic_{b}_by_{u}",
            set_title_fn=lambda b: f"Telemon Cosmetic Forms #{b}",
        )
        print(f"  ✅ {done} done, {fail} failed → {map_path.name}")


async def upload_forms(dry_run: bool = False) -> None:
    await upload_numeric_forms(dry_run)
    await upload_cosmetic_forms(dry_run)


async def _run_slug_upload(
    label: str,
    slugs: list[str],
    sprite_dir: Path,
    map_path_fn,
    set_name_fn,
    set_title_fn,
    base_emoji: str = "🎒",
    dry_run: bool = False,
) -> None:
    """Common flow for slug-based uploads (items, stones)."""
    _print_header(f"{label.upper()} EMOJI UPLOAD")
    print(f"  Entries: {len(slugs)}")

    if not slugs:
        print("  No entries found.")
        return

    if dry_run:
        print("\n📥 DRY RUN: Testing sprite loading...")
        loaded = 0
        for slug in slugs[:20]:
            data = load_local_sprite(sprite_dir / f"{slug}.png")
            if data:
                loaded += 1
                print(f"  ✅ {slug} ({len(data)} bytes)")
            else:
                print(f"  ❌ {slug} — missing")
        print(f"\n  Loaded {loaded}/min({len(slugs)},20) sample sprites")
        return

    sprites = _load_slug_sprites(slugs, sprite_dir, label)
    if not sprites:
        print("  No sprites to upload.")
        return

    available_keys = sorted(sprites.keys())

    for bot in _bots:
        map_path = map_path_fn(bot["bot_id"])
        emoji_map = _load_map(map_path)

        print(f"\n📤 @{bot['username']} (ID {bot['bot_id']})")
        done, fail = await _upload_to_sets(
            label, available_keys, sprites,
            bot["token"], bot["username"],
            emoji_map, map_path,
            set_name_fn, set_title_fn, base_emoji,
        )
        print(f"  ✅ {done} done, {fail} failed → {map_path.name}")


async def upload_items(dry_run: bool = False) -> None:
    await _run_slug_upload(
        "items",
        discover_items(),
        ITEM_SPRITE_DIR,
        item_map_path,
        set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_items_by_{u}" if b == 1 else f"telemon_{SET_VERSION}_items_{b}_by_{u}",
        set_title_fn=lambda b: "Telemon Items",
        base_emoji="🎒",
        dry_run=dry_run,
    )


async def upload_stones(dry_run: bool = False) -> None:
    await _run_slug_upload(
        "stones",
        discover_stones(),
        ITEM_SPRITE_DIR,
        stone_map_path,
        set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_stones_by_{u}" if b == 1 else f"telemon_{SET_VERSION}_stones_{b}_by_{u}",
        set_title_fn=lambda b: "Telemon Stones",
        base_emoji="💎",
        dry_run=dry_run,
    )


async def upload_types(dry_run: bool = False) -> None:
    _print_header("TYPE EMOJI UPLOAD")
    types = discover_types()
    print(f"  Types: {len(types)}")

    if not types:
        print("  No types found.")
        return

    type_names = [name for _, name in types]

    if dry_run:
        print("\n📥 DRY RUN: Testing type sprite loading...")
        for tid, name in types:
            data = load_local_sprite(TYPE_SPRITE_DIR / f"{tid}.png")
            status = f"✅ ({len(data)} bytes)" if data else "❌ missing"
            print(f"  {name} (ID {tid}): {status}")
        return

    # Load type sprites keyed by name (map key), sprite file is {type_id}.png
    sprites: dict[str, bytes] = {}
    for tid, name in types:
        data = load_local_sprite(TYPE_SPRITE_DIR / f"{tid}.png")
        if data:
            sprites[name] = data
        else:
            print(f"  ⚠️  No sprite for type: {name} (ID {tid})")
    print(f"  Loaded {len(sprites)}/{len(types)} type sprites")

    if not sprites:
        print("  No type sprites available.")
        return

    available_keys = sorted(sprites.keys())

    for bot in _bots:
        map_path = type_map_path(bot["bot_id"])
        emoji_map = _load_map(map_path)

        print(f"\n📤 @{bot['username']} (ID {bot['bot_id']})")
        done, fail = await _upload_to_sets(
            "types", available_keys, sprites,
            bot["token"], bot["username"],
            emoji_map, map_path,
            set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_types_by_{u}",
            set_title_fn=lambda b: "Telemon Types",
            base_emoji="⚡",
        )
        print(f"  ✅ {done} done, {fail} failed → {map_path.name}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Remap — re-read existing sticker sets and rebuild maps
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _remap_category(
    label: str,
    keys: list[str],
    emoji_map: dict[str, str],
    bot_token: str,
    username: str,
    set_name_fn,
) -> int:
    """Remap a single category. Returns count of mapped entries."""
    total_batches = (len(keys) + MAX_PER_SET - 1) // MAX_PER_SET if keys else 0
    mapped_count = 0

    search_names = [username] + [u for u in _LEGACY_USERNAMES if u != username]

    async with aiohttp.ClientSession() as http:
        for batch_num in range(1, total_batches + 1):
            batch_start = (batch_num - 1) * MAX_PER_SET
            batch_end = batch_num * MAX_PER_SET
            batch_keys = keys[batch_start:batch_end]
            found = False

            for uname in search_names:
                sname = set_name_fn(batch_num, uname)
                set_data = await tg_get_set(http, bot_token, sname)
                if set_data:
                    stickers = set_data.get("stickers", [])
                    batch_mapped = 0
                    for idx, s in enumerate(stickers):
                        if idx < len(batch_keys):
                            eid = s.get("custom_emoji_id", "")
                            if eid:
                                emoji_map[batch_keys[idx]] = eid
                                batch_mapped += 1
                    mapped_count += batch_mapped
                    print(
                        f"  {label} batch {batch_num} ({sname}): "
                        f"{len(stickers)} stickers → {batch_mapped} mapped"
                    )
                    found = True
                    break

            if not found:
                key_range = f"{batch_keys[0]}…{batch_keys[-1]}" if batch_keys else "?"
                print(f"  {label} batch {batch_num}: NOT FOUND ({key_range})")

    return mapped_count


async def remap_all() -> None:
    _print_header("REMAP ALL EMOJI")

    base_ids = discover_base()
    mega_ids = discover_megas()
    regional_ids = discover_regionals()
    numeric_form_ids = discover_numeric_forms()
    cosmetic_stems = discover_cosmetic_forms()
    item_slugs = discover_items()
    stone_slugs = discover_stones()
    types = discover_types()

    base_keys = [str(pid) for pid in base_ids]
    mega_keys = [str(pid) for pid in mega_ids]
    regional_keys = [str(pid) for pid in regional_ids]
    form_keys = [str(pid) for pid in numeric_form_ids]
    cosmetic_keys = sorted(cosmetic_stems)
    item_keys = sorted(item_slugs)
    stone_keys = sorted(stone_slugs)
    type_keys = sorted(name for _, name in types)

    for bot in _bots:
        bot_token = bot["token"]
        username = bot["username"]
        bot_id = bot["bot_id"]

        print(f"\n📤 @{username} (ID {bot_id})")

        # Pokemon map (base + megas + regionals + numeric forms)
        poke_map: dict[str, str] = {}

        await _remap_category(
            "Base", base_keys, poke_map, bot_token, username,
            set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_base_{b}_by_{u}",
        )
        # Fallback: unversioned naming, then legacy naming
        if not any(k in poke_map for k in base_keys[:5]):
            print("  (Trying unversioned base set naming...)")
            await _remap_category(
                "Base (unversioned)", base_keys, poke_map, bot_token, username,
                set_name_fn=lambda b, u: f"telemon_base_{b}_by_{u}",
            )
        if not any(k in poke_map for k in base_keys[:5]):
            print("  (Trying legacy base set naming...)")
            await _remap_category(
                "Base (legacy)", base_keys, poke_map, bot_token, username,
                set_name_fn=lambda b, u: f"telemon{b}_by_{u}",
            )

        await _remap_category(
            "Mega", mega_keys, poke_map, bot_token, username,
            set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_mega_by_{u}" if b == 1 else f"telemon_{SET_VERSION}_mega_{b}_by_{u}",
        )
        if not any(k in poke_map for k in mega_keys[:3]):
            await _remap_category(
                "Mega (unversioned)", mega_keys, poke_map, bot_token, username,
                set_name_fn=lambda b, u: f"telemon_mega_by_{u}" if b == 1 else f"telemon_mega_{b}_by_{u}",
            )

        await _remap_category(
            "Regional", regional_keys, poke_map, bot_token, username,
            set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_regional_by_{u}" if b == 1 else f"telemon_{SET_VERSION}_regional_{b}_by_{u}",
        )
        if not any(k in poke_map for k in regional_keys[:3]):
            await _remap_category(
                "Regional (unversioned)", regional_keys, poke_map, bot_token, username,
                set_name_fn=lambda b, u: f"telemon_regional_by_{u}" if b == 1 else f"telemon_regional_{b}_by_{u}",
            )

        await _remap_category(
            "Forms", form_keys, poke_map, bot_token, username,
            set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_forms_{b}_by_{u}",
        )
        if not any(k in poke_map for k in form_keys[:3]):
            await _remap_category(
                "Forms (unversioned)", form_keys, poke_map, bot_token, username,
                set_name_fn=lambda b, u: f"telemon_forms_{b}_by_{u}",
            )

        _save_map(pokemon_map_path(bot_id), poke_map)
        print(f"\n  💾 Pokemon map: {len(poke_map)} entries → {pokemon_map_path(bot_id).name}")

        # Cosmetic forms map
        cos_map: dict[str, str] = {}
        await _remap_category(
            "Cosmetic", cosmetic_keys, cos_map, bot_token, username,
            set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_cosmetic_{b}_by_{u}",
        )
        if not cos_map:
            await _remap_category(
                "Cosmetic (unversioned)", cosmetic_keys, cos_map, bot_token, username,
                set_name_fn=lambda b, u: f"telemon_cosmetic_{b}_by_{u}",
            )
        _save_map(form_map_path(bot_id), cos_map)
        print(f"  💾 Form map: {len(cos_map)} entries → {form_map_path(bot_id).name}")

        # Items map
        it_map: dict[str, str] = {}
        await _remap_category(
            "Items", item_keys, it_map, bot_token, username,
            set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_items_by_{u}" if b == 1 else f"telemon_{SET_VERSION}_items_{b}_by_{u}",
        )
        if not it_map:
            await _remap_category(
                "Items (unversioned)", item_keys, it_map, bot_token, username,
                set_name_fn=lambda b, u: f"telemon_items_by_{u}" if b == 1 else f"telemon_items_{b}_by_{u}",
            )
        if not it_map:
            await _remap_category(
                "Items (legacy)", item_keys, it_map, bot_token, username,
                set_name_fn=lambda b, u: f"telemon_items_{b}_by_{u}",
            )
        _save_map(item_map_path(bot_id), it_map)
        print(f"  💾 Item map: {len(it_map)} entries → {item_map_path(bot_id).name}")

        # Stones map
        st_map: dict[str, str] = {}
        await _remap_category(
            "Stones", stone_keys, st_map, bot_token, username,
            set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_stones_by_{u}" if b == 1 else f"telemon_{SET_VERSION}_stones_{b}_by_{u}",
        )
        if not st_map:
            await _remap_category(
                "Stones (unversioned)", stone_keys, st_map, bot_token, username,
                set_name_fn=lambda b, u: f"telemon_stones_by_{u}" if b == 1 else f"telemon_stones_{b}_by_{u}",
            )
        _save_map(stone_map_path(bot_id), st_map)
        print(f"  💾 Stone map: {len(st_map)} entries → {stone_map_path(bot_id).name}")

        # Types map
        ty_map: dict[str, str] = {}
        await _remap_category(
            "Types", type_keys, ty_map, bot_token, username,
            set_name_fn=lambda b, u: f"telemon_{SET_VERSION}_types_by_{u}",
        )
        if not ty_map:
            await _remap_category(
                "Types (unversioned)", type_keys, ty_map, bot_token, username,
                set_name_fn=lambda b, u: f"telemon_types_by_{u}",
            )
        _save_map(type_map_path(bot_id), ty_map)
        print(f"  💾 Type map: {len(ty_map)} entries → {type_map_path(bot_id).name}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Validate — check all mapped emoji still exist
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def validate(fix: bool = False) -> None:
    _print_header("VALIDATE EMOJI MAPS")

    for bot in _bots:
        token = bot["token"]
        bot_id = bot["bot_id"]
        username = bot["username"]

        print(f"\n📤 @{username} (ID {bot_id})")

        map_files = [
            ("pokemon", pokemon_map_path(bot_id)),
            ("forms", form_map_path(bot_id)),
            ("items", item_map_path(bot_id)),
            ("stones", stone_map_path(bot_id)),
            ("types", type_map_path(bot_id)),
        ]

        all_emoji_ids: set[str] = set()
        maps: dict[str, tuple[Path, dict[str, str]]] = {}

        for label, path in map_files:
            data = _load_map(path)
            if data:
                maps[label] = (path, data)
                all_emoji_ids.update(data.values())

        total = len(all_emoji_ids)
        print(f"  Total unique emoji IDs to check: {total}")

        if not total:
            print("  No emoji to validate.")
            continue

        async with aiohttp.ClientSession() as http:
            valid = await tg_check_emoji(http, token, list(all_emoji_ids))

        missing = all_emoji_ids - valid
        print(f"  ✅ Valid: {len(valid)}")
        print(f"  ❌ Missing: {len(missing)}")

        if missing:
            for label, (path, data) in maps.items():
                dead_keys = [k for k, v in data.items() if v in missing]
                if dead_keys:
                    print(f"\n  [{label}] {len(dead_keys)} dead entries:")
                    for k in dead_keys[:10]:
                        print(f"    {k} → {data[k]}")
                    if len(dead_keys) > 10:
                        print(f"    ... and {len(dead_keys) - 10} more")

                    if fix:
                        for k in dead_keys:
                            del data[k]
                        _save_map(path, data)
                        print(f"    🔧 Removed {len(dead_keys)} dead entries from {path.name}")

        if not missing:
            print("  All emoji are valid! ✅")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Delete old — remove ALL sticker sets & clear maps
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _all_set_name_patterns(batch: int, uname: str) -> list[str]:
    """Generate all known set name patterns for a given batch & username."""
    names = []
    # Legacy patterns (v1 era)
    names.append(f"telemon{batch}_by_{uname}")
    for v in ["v2", "v3", "v4"]:
        names.append(f"telemon_emoji_{v}_{batch}_by_{uname}")
    # Unversioned category patterns
    names.append(f"telemon_base_{batch}_by_{uname}")
    if batch == 1:
        names.append(f"telemon_mega_by_{uname}")
        names.append(f"telemon_regional_by_{uname}")
        names.append(f"telemon_items_by_{uname}")
        names.append(f"telemon_stones_by_{uname}")
        names.append(f"telemon_types_by_{uname}")
    names.append(f"telemon_mega_{batch}_by_{uname}")
    names.append(f"telemon_regional_{batch}_by_{uname}")
    names.append(f"telemon_forms_{batch}_by_{uname}")
    names.append(f"telemon_cosmetic_{batch}_by_{uname}")
    names.append(f"telemon_items_{batch}_by_{uname}")
    names.append(f"telemon_stones_{batch}_by_{uname}")
    # Current versioned patterns
    names.append(f"telemon_{SET_VERSION}_base_{batch}_by_{uname}")
    if batch == 1:
        names.append(f"telemon_{SET_VERSION}_mega_by_{uname}")
        names.append(f"telemon_{SET_VERSION}_regional_by_{uname}")
        names.append(f"telemon_{SET_VERSION}_items_by_{uname}")
        names.append(f"telemon_{SET_VERSION}_stones_by_{uname}")
        names.append(f"telemon_{SET_VERSION}_types_by_{uname}")
    names.append(f"telemon_{SET_VERSION}_mega_{batch}_by_{uname}")
    names.append(f"telemon_{SET_VERSION}_regional_{batch}_by_{uname}")
    names.append(f"telemon_{SET_VERSION}_forms_{batch}_by_{uname}")
    names.append(f"telemon_{SET_VERSION}_cosmetic_{batch}_by_{uname}")
    names.append(f"telemon_{SET_VERSION}_items_{batch}_by_{uname}")
    names.append(f"telemon_{SET_VERSION}_stones_{batch}_by_{uname}")
    return names


async def delete_old() -> None:
    _print_header("DELETE ALL STICKER SETS & CLEAR MAPS")

    for bot in _bots:
        token = bot["token"]
        username = bot["username"]
        bot_id = bot["bot_id"]

        print(f"\n📤 @{username} (ID {bot_id})")

        search_usernames = [username] + [
            u for u in _LEGACY_USERNAMES if u != username
        ]

        deleted = 0
        skipped = 0
        async with aiohttp.ClientSession() as http:
            for uname in search_usernames:
                is_own = uname == username
                for n in range(1, 15):
                    for sname in _all_set_name_patterns(n, uname):
                        set_data = await tg_get_set(http, token, sname)
                        if set_data:
                            ok = await tg_delete_set(http, token, sname)
                            count = len(set_data.get("stickers", []))
                            if ok:
                                deleted += 1
                                print(f"  {sname}: {count} stickers → deleted")
                            elif is_own:
                                print(f"  {sname}: {count} stickers → FAILED")
                            else:
                                skipped += 1
                            await asyncio.sleep(0.2)

        msg = f"\n  Deleted {deleted} sets for @{username}"
        if skipped:
            msg += f" (skipped {skipped} sets owned by other bots)"
        print(msg)

    # Clear ALL map files
    print("\n  Clearing map files...")
    for bot in _bots:
        bot_id = bot["bot_id"]
        for path in [
            pokemon_map_path(bot_id),
            form_map_path(bot_id),
            item_map_path(bot_id),
            stone_map_path(bot_id),
            type_map_path(bot_id),
        ]:
            if path.exists():
                _save_map(path, {})
                print(f"  Cleared {path.name}")

    # Also clear generic (no bot_id) map files
    for pattern in ["emoji_map.json", "item_emoji_map.json", "type_emoji_map.json"]:
        p = DATA_DIR / pattern
        if p.exists():
            _save_map(p, {})
            print(f"  Cleared {p.name}")

    # Remove legacy backup files
    for old_file in DATA_DIR.glob("emoji_map_old*.json"):
        old_file.unlink()
        print(f"  Removed {old_file.name}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Status — show current map statistics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def show_status() -> None:
    _print_header("EMOJI MAP STATUS")

    # Show discovery counts
    base = discover_base()
    megas = discover_megas()
    regionals = discover_regionals()
    num_forms = discover_numeric_forms()
    cos_forms = discover_cosmetic_forms()
    items = discover_items()
    stones = discover_stones()
    types = discover_types()

    print("\n  📊 Discoverable sprites:")
    print(f"    Base Pokemon:     {len(base)}")
    print(f"    Megas:            {len(megas)}")
    print(f"    Regionals:        {len(regionals)}")
    print(f"    Numeric forms:    {len(num_forms)}")
    print(f"    Cosmetic forms:   {len(cos_forms)}")
    print(f"    Items:            {len(items)}")
    print(f"    Stones:           {len(stones)}")
    print(f"    Types:            {len(types)}")
    total = len(base) + len(megas) + len(regionals) + len(num_forms) + len(cos_forms) + len(items) + len(stones) + len(types)
    print(f"    ─────────────────────")
    print(f"    Total:            {total}")

    # Show per-bot map stats
    bot_ids: set[int] = set()
    for p in DATA_DIR.glob("*emoji_map_*.json"):
        m = re.search(r"_(\d{5,})\.json$", p.name)
        if m:
            bot_ids.add(int(m.group(1)))
    for bot_id in sorted(bot_ids):

        print(f"\n  📦 Bot ID {bot_id}:")
        for label, path_fn in [
            ("Pokemon", pokemon_map_path),
            ("Forms", form_map_path),
            ("Items", item_map_path),
            ("Stones", stone_map_path),
            ("Types", type_map_path),
        ]:
            path = path_fn(bot_id)
            data = _load_map(path)
            if data:
                print(f"    {label:12s} {len(data):5d} entries  ({path.name})")
            else:
                print(f"    {label:12s}     0 entries  ({path.name})")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HELP_TEXT = """\
Usage: python -m telemon.scripts.upload_emoji <command> [--bot <id>] [--dry-run] [--fix]

Commands:
  base       Upload base Pokemon (1-1025, 6 sets)
  megas      Upload mega evolutions (1 set)
  regionals  Upload regional forms (1 set)
  forms      Upload alt forms — numeric + cosmetic (2-3 sets)
  items      Upload non-stone items (1 set)
  stones     Upload evo + mega stones (1 set)
  types      Upload type icons (1 set)
  all        Upload everything
  fresh      Delete all sets, clear maps, then upload everything from scratch
  remap      Re-read existing sticker sets & rebuild all maps
  validate   Check all mapped emoji still exist on Telegram
  delete-old Delete ALL sticker sets (legacy + current) & clear maps
  status     Show current map statistics
  help       Show this help

Options:
  --bot <id>  Target a specific bot: 1 or 2 (by position), a bot ID,
              or "both" (default). Omit to upload to all bots.
  --dry-run   Test sprite loading without uploading
  --fix       With validate: remove dead entries from maps
"""


async def main() -> None:
    args = sys.argv[1:] if len(sys.argv) > 1 else ["help"]
    dry_run = "--dry-run" in args
    fix = "--fix" in args

    # Parse --bot <value>
    bot_filter: str | None = None
    filtered_args: list[str] = []
    skip_next = False
    for i, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if a == "--bot":
            if i + 1 < len(args):
                bot_filter = args[i + 1]
                skip_next = True
            else:
                print("Error: --bot requires a value (1, 2, a bot ID, or 'both')")
                return
        elif a.startswith("--bot="):
            bot_filter = a.split("=", 1)[1]
        elif not a.startswith("--"):
            filtered_args.append(a)
    args = filtered_args

    if not args or args[0] == "help":
        print(HELP_TEXT)
        return

    cmd = args[0].lower()

    if cmd == "status":
        show_status()
        return

    t0 = time.time()
    await _init_bots(bot_filter)

    if cmd == "base":
        await upload_base(dry_run)
    elif cmd == "megas":
        await upload_megas(dry_run)
    elif cmd == "regionals":
        await upload_regionals(dry_run)
    elif cmd == "forms":
        await upload_forms(dry_run)
    elif cmd == "items":
        await upload_items(dry_run)
    elif cmd == "stones":
        await upload_stones(dry_run)
    elif cmd == "types":
        await upload_types(dry_run)
    elif cmd == "all":
        await upload_base(dry_run)
        await upload_megas(dry_run)
        await upload_regionals(dry_run)
        await upload_forms(dry_run)
        await upload_items(dry_run)
        await upload_stones(dry_run)
        await upload_types(dry_run)
    elif cmd == "fresh":
        await delete_old()
        print("\n" + "=" * 60)
        print("  FRESH UPLOAD — all sets deleted, maps cleared")
        print("=" * 60)
        await upload_base(dry_run)
        await upload_megas(dry_run)
        await upload_regionals(dry_run)
        await upload_forms(dry_run)
        await upload_items(dry_run)
        await upload_stones(dry_run)
        await upload_types(dry_run)
    elif cmd == "remap":
        await remap_all()
    elif cmd == "validate":
        await validate(fix=fix)
    elif cmd == "delete-old":
        await delete_old()
    else:
        print(f"Unknown command: {cmd}")
        print(HELP_TEXT)
        return

    print(f"\n⏱  Total time: {elapsed_str(t0)}")


if __name__ == "__main__":
    asyncio.run(main())
