"""Upload Pokemon, item, and type sprites as Telegram custom emoji.

Creates custom emoji sticker sets and saves ID mappings to JSON files:
  - data/emoji_map.json       — dex_number  → custom_emoji_id  (Pokemon)
  - data/item_emoji_map.json  — item_slug   → custom_emoji_id  (Items)
  - data/type_emoji_map.json  — type_name   → custom_emoji_id  (Types)

Usage:
    python -m telemon.scripts.upload_emoji pokemon   — upload Pokemon sprites
    python -m telemon.scripts.upload_emoji items     — upload item sprites
    python -m telemon.scripts.upload_emoji types     — upload type sprites
    python -m telemon.scripts.upload_emoji all       — upload everything
    python -m telemon.scripts.upload_emoji remap     — re-read existing sets & fix mapping
    python -m telemon.scripts.upload_emoji --dry-run — test sprite loading only

Resumable: skips entries already in the JSON maps.
"""

import asyncio
import csv
import io
import json
import sys
import time
from pathlib import Path

import aiohttp
from PIL import Image

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
# Legacy bot usernames — searched during remap to find old sticker sets
_LEGACY_USERNAMES = ["TelemonXRobot"]

# Populated at runtime via getMe for each token in .env
_bots: list[dict] = []  # [{"token": str, "username": str, "bot_id": int}, ...]
OWNER_ID = 6894738352
MAX_PER_SET = 200
SPRITE_SIZE = 100  # 100×100 px for custom emoji

# Throttle: max concurrent sticker API calls + pacing delay
_API_CONCURRENCY = 3
_API_PACE = 0.12  # seconds between API calls (per-slot)
_api_sem: asyncio.Semaphore | None = None
_last_call_time: float = 0.0
_throttle_lock: asyncio.Lock | None = None

DATA_DIR = Path(__file__).parent.parent.parent.parent / "data"
CSV_DIR = DATA_DIR / "csv"

# Per-bot output map files
def pokemon_map_path(bot_id: int) -> Path:
    return DATA_DIR / f"emoji_map_{bot_id}.json"

def item_map_path(bot_id: int) -> Path:
    return DATA_DIR / f"item_emoji_map_{bot_id}.json"

def type_map_path(bot_id: int) -> Path:
    return DATA_DIR / f"type_emoji_map_{bot_id}.json"

# Sprite directories
POKEMON_SPRITE_DIR = DATA_DIR / "sprites" / "pokemon"
ITEM_SPRITE_DIR = DATA_DIR / "sprites" / "items"
TYPE_SPRITE_DIR = DATA_DIR / "sprites" / "types" / "generation-ix" / "scarlet-violet"

# Remote fallback
SPRITE_REMOTE_BASE = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/{pid}.png"


# ──────────────────────────────────────────────
# CSV data loaders
# ──────────────────────────────────────────────
def load_pokemon_csv() -> list[dict]:
    """Load pokemon.csv — returns list of {id, identifier, species_id, is_default}."""
    path = CSV_DIR / "pokemon.csv"
    if not path.exists():
        return []
    with open(path) as f:
        return [
            {
                "id": int(r["id"]),
                "identifier": r["identifier"],
                "species_id": int(r["species_id"]),
                "is_default": int(r["is_default"]),
            }
            for r in csv.DictReader(f)
        ]


def load_types_csv() -> dict[int, str]:
    """Load types.csv — returns {type_id: type_name}."""
    path = CSV_DIR / "types.csv"
    if not path.exists():
        return {}
    with open(path) as f:
        return {int(r["id"]): r["identifier"] for r in csv.DictReader(f)}


def get_pokemon_default_forms() -> dict[int, dict]:
    """Get default-form Pokemon: {species_id: {id, identifier}}.

    Uses pokemon.csv to correctly resolve which sprite file
    corresponds to each species.
    """
    rows = load_pokemon_csv()
    result = {}
    for r in rows:
        if r["is_default"] == 1 and r["species_id"] <= 1025:
            result[r["species_id"]] = {
                "id": r["id"],
                "identifier": r["identifier"],
            }
    return result


def get_bot_items() -> list[dict]:
    """Get item identifiers used by the bot (from core/items.py).

    Returns list of {id, name, slug} for items that have local sprites.
    """
    import re

    items_py = Path(__file__).parent.parent / "core" / "items.py"
    if not items_py.exists():
        return []

    content = items_py.read_text()
    # Extract item names from the ALL_ITEMS list
    names = re.findall(r'"name":\s*"([^"]+)"', content)
    ids = re.findall(r'"id":\s*(\d+)', content)

    result = []
    for i, name in enumerate(names):
        slug = name.lower().replace("'", "").replace(" ", "-")
        item_id = int(ids[i]) if i < len(ids) else i + 1
        sprite_path = ITEM_SPRITE_DIR / f"{slug}.png"
        if sprite_path.exists():
            result.append({"id": item_id, "name": name, "slug": slug})
    return result


# ──────────────────────────────────────────────
# Sprite loading
# ──────────────────────────────────────────────
def process_sprite(raw: bytes) -> bytes | None:
    """Process raw sprite PNG data: crop, resize to SPRITE_SIZE×SPRITE_SIZE."""
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGBA")

        # Crop transparent padding
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)

        # Scale to fit SPRITE_SIZE
        ratio = min(SPRITE_SIZE / img.width, SPRITE_SIZE / img.height)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.NEAREST)

        # Center on transparent canvas
        final = Image.new("RGBA", (SPRITE_SIZE, SPRITE_SIZE), (0, 0, 0, 0))
        offset = ((SPRITE_SIZE - new_size[0]) // 2, (SPRITE_SIZE - new_size[1]) // 2)
        final.paste(img, offset)

        buf = io.BytesIO()
        final.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        print(f"  [WARN] Failed to process sprite: {e}")
        return None


def load_local_sprite(path: Path) -> bytes | None:
    """Load and process a local sprite file."""
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        if len(raw) < 100:
            return None
        return process_sprite(raw)
    except Exception:
        return None


async def load_pokemon_sprite(
    http: aiohttp.ClientSession, pokemon_id: int
) -> bytes | None:
    """Load Pokemon sprite — local first, remote fallback. Retries until success."""
    # Try local
    data = load_local_sprite(POKEMON_SPRITE_DIR / f"{pokemon_id}.png")
    if data:
        return data

    # Remote fallback with retry
    url = SPRITE_REMOTE_BASE.format(pid=pokemon_id)
    attempt = 0
    max_format_retries = 5  # Cap retries for format errors (not transient)
    while True:
        attempt += 1
        try:
            async with http.get(url) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                raw = await resp.read()
                if len(raw) < 100:
                    raise Exception("Response too small")
                result = process_sprite(raw)
                if result:
                    return result
                # Image format unsupported (SVG, GIF, etc.) — cap retries
                if attempt >= max_format_retries:
                    print(f"  [SKIP] Sprite {pokemon_id}: unsupported format after {attempt} tries")
                    return None
                raise Exception("process_sprite returned None")
        except Exception as e:
            if attempt >= max_format_retries and "process_sprite" in str(e):
                return None
            wait = min(2 ** attempt, 30)
            if attempt <= 3:
                print(f"  [WARN] Sprite {pokemon_id} attempt {attempt}: {e} — retry in {wait}s")
            await asyncio.sleep(wait)


# ──────────────────────────────────────────────
# Display helpers
# ──────────────────────────────────────────────
def progress_bar(current: int, total: int, width: int = 30) -> str:
    filled = int(current / total * width) if total else 0
    bar = "█" * filled + "░" * (width - filled)
    pct = current / total * 100 if total else 0
    return f"[{bar}] {pct:5.1f}%  ({current}/{total})"


def elapsed_str(start: float) -> str:
    secs = int(time.time() - start)
    m, s = divmod(secs, 60)
    return f"{m:02d}:{s:02d}"


def eta_str(start: float, done: int, total: int) -> str:
    if done == 0:
        return "--:--"
    elapsed = time.time() - start
    rate = done / elapsed
    remaining = (total - done) / rate
    m, s = divmod(int(remaining), 60)
    return f"~{m:02d}:{s:02d}"


# ──────────────────────────────────────────────
# Telegram API helpers
# ──────────────────────────────────────────────
def get_all_bot_tokens() -> list[str]:
    """Read all bot tokens from .env (BOT_TOKEN, PREMIUM_BOT_TOKEN, etc.)."""
    env_path = DATA_DIR.parent / ".env"
    tokens = []
    seen = set()
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


async def _init_all_bots() -> list[dict]:
    """Fetch username for each unique bot token via getMe API."""
    global _bots
    tokens = get_all_bot_tokens()
    
    _bots = []
    async with aiohttp.ClientSession() as http:
        for token in tokens:
            resp = await http.get(f"https://api.telegram.org/bot{token}/getMe")
            result = await resp.json()
            if result.get("ok"):
                info = result["result"]
                _bots.append({
                    "token": token,
                    "username": info["username"],
                    "bot_id": info["id"],
                })
                print(f"  Bot: @{info['username']} (ID {info['id']})")
            else:
                print(f"  [WARN] getMe failed for a token: {result.get('description')}")
    if not _bots:
        raise RuntimeError("No valid bot tokens found")
    return _bots


def _all_known_usernames() -> list[str]:
    """Return all usernames to search for sticker sets (current bots + legacy)."""
    names = [b["username"] for b in _bots]
    for legacy in _LEGACY_USERNAMES:
        if legacy not in names:
            names.append(legacy)
    return names


async def _throttle() -> None:
    """Global API throttle: limits concurrency + pacing."""
    global _last_call_time
    assert _api_sem is not None and _throttle_lock is not None
    async with _throttle_lock:
        now = time.time()
        wait = _API_PACE - (now - _last_call_time)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_time = time.time()


async def create_emoji_set(
    http: aiohttp.ClientSession,
    bot_token: str,
    set_name: str,
    title: str,
    png_data: bytes,
    base_emoji: str = "🔴",
) -> bool:
    """Create a new custom emoji sticker set with one initial sticker."""
    async with _api_sem:
        await _throttle()
        form = aiohttp.FormData()
        form.add_field("user_id", str(OWNER_ID))
        form.add_field("name", set_name)
        form.add_field("title", title)
        form.add_field("sticker_type", "custom_emoji")
        form.add_field(
            "stickers",
            json.dumps(
                [
                    {
                        "sticker": "attach://file0",
                        "emoji_list": [base_emoji],
                        "format": "static",
                    }
                ]
            ),
        )
        form.add_field("file0", png_data, filename="sprite.png", content_type="image/png")

        resp = await http.post(
            f"https://api.telegram.org/bot{bot_token}/createNewStickerSet",
            data=form,
        )
        result = await resp.json()
    if not result.get("ok"):
        desc = result.get("description", "")
        if "Too Many Requests" in desc or result.get("error_code") == 429:
            retry_after = result.get("parameters", {}).get("retry_after", 5)
            print(f"  ⏳ Rate limited — waiting {retry_after}s...")
            await asyncio.sleep(retry_after + 1)
            return await create_emoji_set(
                http, bot_token, set_name, title, png_data, base_emoji
            )
        print(f"  [ERR] createNewStickerSet: {desc}")
        return False
    return True


async def add_emoji_to_set(
    http: aiohttp.ClientSession,
    bot_token: str,
    set_name: str,
    png_data: bytes,
    base_emoji: str = "🔴",
) -> bool:
    """Add one sticker to an existing set."""
    async with _api_sem:
        await _throttle()
        form = aiohttp.FormData()
        form.add_field("user_id", str(OWNER_ID))
        form.add_field("name", set_name)
        form.add_field(
            "sticker",
            json.dumps(
                {
                    "sticker": "attach://file0",
                    "emoji_list": [base_emoji],
                    "format": "static",
                }
            ),
        )
        form.add_field("file0", png_data, filename="sprite.png", content_type="image/png")

        resp = await http.post(
            f"https://api.telegram.org/bot{bot_token}/addStickerToSet",
            data=form,
        )
        result = await resp.json()
    if not result.get("ok"):
        desc = result.get("description", "")
        if "Too Many Requests" in desc or result.get("error_code") == 429:
            retry_after = result.get("parameters", {}).get("retry_after", 5)
            print(f"  ⏳ Rate limited — waiting {retry_after}s...")
            await asyncio.sleep(retry_after + 1)
            return await add_emoji_to_set(
                http, bot_token, set_name, png_data, base_emoji
            )
        print(f"  [ERR] addStickerToSet: {desc}")
        return False
    return True


async def get_sticker_set(
    http: aiohttp.ClientSession,
    bot_token: str,
    set_name: str,
) -> dict | None:
    """Get sticker set data from Telegram."""
    resp = await http.get(
        f"https://api.telegram.org/bot{bot_token}/getStickerSet",
        params={"name": set_name},
    )
    result = await resp.json()
    if not result.get("ok"):
        return None
    return result["result"]


async def get_last_emoji_id(
    http: aiohttp.ClientSession,
    bot_token: str,
    set_name: str,
) -> str | None:
    """Get the custom_emoji_id of the last sticker in a set.

    This is used right after uploading a sticker to correctly map it.
    """
    data = await get_sticker_set(http, bot_token, set_name)
    if not data or not data.get("stickers"):
        return None
    return data["stickers"][-1].get("custom_emoji_id")


# ──────────────────────────────────────────────
# Upload: Pokemon sprites
# ──────────────────────────────────────────────
def pokemon_set_name(batch: int, username: str) -> str:
    return f"telemon{batch}_by_{username}"


def pokemon_set_title(batch: int, start: int, end: int) -> str:
    return f"Telemon #{start:04d}-{end:04d}"


async def upload_pokemon(dry_run: bool = False) -> None:
    """Upload all Pokemon sprites and build per-bot emoji_map_{bot_id}.json."""
    print("\n" + "=" * 60)
    print("  POKEMON EMOJI UPLOAD")
    print("=" * 60)

    forms = get_pokemon_default_forms()
    total = len(forms)
    print(f"  Species from pokemon.csv: {total}")

    if dry_run:
        print("\n📥 DRY RUN: Loading sprites (no upload)...")
        loaded = 0
        async with aiohttp.ClientSession() as http:
            for dex in sorted(forms.keys())[:20]:
                info = forms[dex]
                data = await load_pokemon_sprite(http, info["id"])
                if data:
                    loaded += 1
                    print(f"  ✅ #{dex:04d} {info['identifier']} ({len(data)} bytes)")
                else:
                    print(f"  ❌ #{dex:04d} {info['identifier']} — sprite missing")
        print(f"\n  Loaded {loaded}/20 sample sprites")
        return

    # Phase 1: Download ALL sprites once (shared across bots)
    all_dex = sorted(forms.keys())
    print(f"\n📥 PHASE 1: Loading {len(all_dex)} sprites...\n")
    sprites: dict[int, bytes] = {}
    dl_start = time.time()

    async with aiohttp.ClientSession() as http:
        for i, dex in enumerate(all_dex):
            info = forms[dex]
            data = await load_pokemon_sprite(http, info["id"])
            if data:
                sprites[dex] = data
            done = i + 1
            if done % 50 == 0 or done == len(all_dex):
                print(
                    f"  {progress_bar(done, len(all_dex))}  "
                    f"elapsed {elapsed_str(dl_start)}  "
                    f"ETA {eta_str(dl_start, done, len(all_dex))}"
                )

    failed_dl = len(all_dex) - len(sprites)
    print(f"\n  ✅ Loaded: {len(sprites)}  |  ❌ Failed: {failed_dl}")

    if not sprites:
        print("\nNo sprites to upload. Exiting.")
        return

    # Phase 2: Upload to each bot with per-bot maps
    def batch_for(dex: int) -> int:
        return (dex - 1) // MAX_PER_SET + 1

    for bot in _bots:
        bot_token = bot["token"]
        bot_id = bot["bot_id"]
        username = bot["username"]
        map_file = pokemon_map_path(bot_id)

        # Load this bot's existing map
        emoji_map: dict[str, str] = {}
        if map_file.exists():
            emoji_map = json.loads(map_file.read_text())
        already = {int(k) for k in emoji_map}
        remaining_dex = sorted(d for d in sprites if d not in already)

        print(f"\n📤 @{username} (ID {bot_id})")
        print(f"  Already mapped: {len(already)}")
        print(f"  Remaining: {len(remaining_dex)}")

        if not remaining_dex:
            print("  ✅ All Pokemon already mapped for this bot!")
            continue

        batches: dict[int, list[int]] = {}
        for dex in remaining_dex:
            b = batch_for(dex)
            batches.setdefault(b, []).append(dex)

        print(f"  {len(batches)} batches running concurrently\n")
        upload_start = time.time()
        _progress = {"done": 0, "fail": 0, "total": len(remaining_dex)}

        async def _upload_batch(batch_num: int, dex_list: list[int]) -> None:
            """Upload one batch of stickers sequentially, map at end."""
            sname = pokemon_set_name(batch_num, username)
            batch_start_dex = (batch_num - 1) * MAX_PER_SET + 1
            batch_end_dex = min(batch_num * MAX_PER_SET, 1025)
            title = pokemon_set_title(batch_num, batch_start_dex, batch_end_dex)

            async with aiohttp.ClientSession() as http:
                set_data = await get_sticker_set(http, bot_token, sname)
                set_exists = set_data is not None
                existing_count = len(set_data.get("stickers", [])) if set_data else 0

                # Build the full ordered dex list for this batch
                # (all dex with sprites, in sorted order)
                full_batch_dex = sorted(
                    d for d in sprites
                    if batch_start_dex <= d <= batch_end_dex
                )

                if set_exists and existing_count >= len(full_batch_dex):
                    # Full — identity remap using actual dex list
                    stickers = set_data.get("stickers", [])
                    mapped = 0
                    for idx, s in enumerate(stickers):
                        if idx < len(full_batch_dex):
                            eid = s.get("custom_emoji_id", "")
                            if eid:
                                emoji_map[str(full_batch_dex[idx])] = eid
                                mapped += 1
                    _progress["done"] += mapped
                    print(
                        f"  Set {batch_num}: full ({existing_count}) → remapped {mapped}"
                    )
                    map_file.write_text(json.dumps(emoji_map, indent=2))
                    return

                if set_exists:
                    print(f"  Set {batch_num}: exists ({existing_count} stickers)")

                uploaded_dex = []
                for dex in dex_list:
                    png = sprites[dex]
                    if not set_exists:
                        ok = await create_emoji_set(
                            http, bot_token, sname, title, png
                        )
                        if ok:
                            set_exists = True
                            uploaded_dex.append(dex)
                            _progress["done"] += 1
                        else:
                            _progress["fail"] += 1
                    else:
                        ok = await add_emoji_to_set(http, bot_token, sname, png)
                        if ok:
                            uploaded_dex.append(dex)
                            _progress["done"] += 1
                        else:
                            _progress["fail"] += 1

                    if _progress["done"] % 25 == 0:
                        print(
                            f"  {progress_bar(_progress['done'], _progress['total'])}  "
                            f"elapsed {elapsed_str(upload_start)}  "
                            f"ETA {eta_str(upload_start, _progress['done'], _progress['total'])}"
                        )

                # Identity-based mapping: position → actual dex number
                set_data = await get_sticker_set(http, bot_token, sname)
                if set_data:
                    stickers = set_data.get("stickers", [])
                    for idx, s in enumerate(stickers):
                        if idx < len(full_batch_dex):
                            eid = s.get("custom_emoji_id", "")
                            if eid:
                                emoji_map[str(full_batch_dex[idx])] = eid

                map_file.write_text(json.dumps(emoji_map, indent=2))
                print(
                    f"  Set {batch_num} done: {len(uploaded_dex)} uploaded, "
                    f"{len(emoji_map)} total mapped"
                )

        await asyncio.gather(
            *[_upload_batch(bn, dl) for bn, dl in sorted(batches.items())]
        )

        map_file.write_text(json.dumps(emoji_map, indent=2))
        print(
            f"\n  ✅ @{username}: {_progress['done']} done, "
            f"{_progress['fail']} failed  →  {map_file.name}"
        )


# ──────────────────────────────────────────────
# Upload: Item sprites
# ──────────────────────────────────────────────
def item_set_name(batch: int, username: str) -> str:
    return f"telemon_items_{batch}_by_{username}"


async def upload_items(dry_run: bool = False) -> None:
    """Upload item sprites — per-bot item_emoji_map_{bot_id}.json."""
    print("\n" + "=" * 60)
    print("  ITEM EMOJI UPLOAD")
    print("=" * 60)

    items = get_bot_items()
    print(f"  Bot items with sprites: {len(items)}")

    if dry_run:
        print("\n📥 DRY RUN: Testing sprite loading...")
        for it in items[:10]:
            data = load_local_sprite(ITEM_SPRITE_DIR / f"{it['slug']}.png")
            status = f"✅ ({len(data)} bytes)" if data else "❌ missing"
            print(f"  {it['slug']}: {status}")
        return

    # Load all item sprites once
    all_sprites: dict[str, bytes] = {}
    for it in items:
        data = load_local_sprite(ITEM_SPRITE_DIR / f"{it['slug']}.png")
        if data:
            all_sprites[it["slug"]] = data
    print(f"  Loaded {len(all_sprites)}/{len(items)} sprites")

    if not all_sprites:
        print("No item sprites available.")
        return

    for bot in _bots:
        bot_token = bot["token"]
        bot_id = bot["bot_id"]
        username = bot["username"]
        map_file = item_map_path(bot_id)

        item_map: dict[str, str] = {}
        if map_file.exists():
            item_map = json.loads(map_file.read_text())
        already = set(item_map.keys())
        sprites = {k: v for k, v in all_sprites.items() if k not in already}

        print(f"\n📤 @{username} (ID {bot_id})")
        print(f"  Already mapped: {len(already)}")
        print(f"  Remaining: {len(sprites)}")

        if not sprites:
            print("  ✅ All items already mapped for this bot!")
            continue

        upload_start = time.time()
        sname = item_set_name(1, username)
        uploaded_slugs = []
        sorted_slugs = sorted(sprites.keys())

        async with aiohttp.ClientSession() as http:
            set_data = await get_sticker_set(http, bot_token, sname)
            set_exists = set_data is not None
            existing_count = len(set_data.get("stickers", [])) if set_data else 0

            if set_exists and existing_count >= len(all_sprites):
                # Full — remap
                stickers = set_data.get("stickers", [])
                all_sorted = sorted(all_sprites.keys())
                for idx, s in enumerate(stickers):
                    if idx < len(all_sorted):
                        eid = s.get("custom_emoji_id", "")
                        if eid:
                            item_map[all_sorted[idx]] = eid
                print(f"  Set full ({existing_count}) → remapped {len(item_map)}")
            else:
                if set_exists:
                    print(f"  Set exists with {existing_count} stickers")

                for i, (slug, png) in enumerate(sorted(sprites.items())):
                    if not set_exists:
                        ok = await create_emoji_set(
                            http, bot_token, sname, "Telemon Items", png, "🎒"
                        )
                        if ok:
                            set_exists = True
                            uploaded_slugs.append(slug)
                    else:
                        ok = await add_emoji_to_set(http, bot_token, sname, png, "🎒")
                        if ok:
                            uploaded_slugs.append(slug)

                    if (i + 1) % 10 == 0 or (i + 1) == len(sprites):
                        print(
                            f"  {progress_bar(i + 1, len(sprites))}  "
                            f"elapsed {elapsed_str(upload_start)}"
                        )

                # Map positions at end
                set_data = await get_sticker_set(http, bot_token, sname)
                if set_data:
                    stickers = set_data.get("stickers", [])
                    all_sorted = sorted(all_sprites.keys())
                    for idx, s in enumerate(stickers):
                        if idx < len(all_sorted):
                            eid = s.get("custom_emoji_id", "")
                            if eid:
                                item_map[all_sorted[idx]] = eid

        map_file.write_text(json.dumps(item_map, indent=2))
        print(f"  💾 Saved {map_file.name} ({len(item_map)} items)")


# ──────────────────────────────────────────────
# Upload: Type sprites
# ──────────────────────────────────────────────
async def upload_types(dry_run: bool = False) -> None:
    """Upload type icon sprites — per-bot type_emoji_map_{bot_id}.json."""
    print("\n" + "=" * 60)
    print("  TYPE EMOJI UPLOAD")
    print("=" * 60)

    types = load_types_csv()
    main_types = {tid: name for tid, name in types.items() if 1 <= tid <= 18}
    print(f"  Types: {len(main_types)}")

    if dry_run:
        print("\n📥 DRY RUN: Testing type sprite loading...")
        for tid, name in sorted(main_types.items()):
            data = load_local_sprite(TYPE_SPRITE_DIR / f"{tid}.png")
            status = f"✅ ({len(data)} bytes)" if data else "❌ missing"
            print(f"  {name} (ID {tid}): {status}")
        return

    # Load all type sprites once
    all_sprites: dict[str, bytes] = {}
    for tid, name in sorted(main_types.items()):
        data = load_local_sprite(TYPE_SPRITE_DIR / f"{tid}.png")
        if data:
            all_sprites[name] = data
        else:
            print(f"  ⚠️  No sprite for type: {name} (ID {tid})")
    print(f"  Loaded {len(all_sprites)}/{len(main_types)} type sprites")

    if not all_sprites:
        print("No type sprites available.")
        return

    for bot in _bots:
        bot_token = bot["token"]
        bot_id = bot["bot_id"]
        username = bot["username"]
        map_file = type_map_path(bot_id)

        type_map: dict[str, str] = {}
        if map_file.exists():
            type_map = json.loads(map_file.read_text())
        already = set(type_map.keys())
        sprites = {k: v for k, v in all_sprites.items() if k not in already}

        print(f"\n📤 @{username} (ID {bot_id})")
        print(f"  Already mapped: {len(already)}")
        print(f"  Remaining: {len(sprites)}")

        if not sprites:
            print("  ✅ All types already mapped for this bot!")
            continue

        sname = f"telemon_types_by_{username}"
        uploaded_names = []
        sorted_names = sorted(sprites.keys())

        async with aiohttp.ClientSession() as http:
            set_data = await get_sticker_set(http, bot_token, sname)
            set_exists = set_data is not None
            existing_count = len(set_data.get("stickers", [])) if set_data else 0

            if set_exists and existing_count >= len(all_sprites):
                # Full — remap
                stickers = set_data.get("stickers", [])
                all_sorted = sorted(all_sprites.keys())
                for idx, s in enumerate(stickers):
                    if idx < len(all_sorted):
                        eid = s.get("custom_emoji_id", "")
                        if eid:
                            type_map[all_sorted[idx]] = eid
                print(f"  Set full ({existing_count}) → remapped {len(type_map)}")
            else:
                if set_exists:
                    print(f"  Set exists with {existing_count} stickers")

                for i, (name, png) in enumerate(sorted(sprites.items())):
                    if not set_exists:
                        ok = await create_emoji_set(
                            http, bot_token, sname, "Telemon Types", png, "⚡"
                        )
                        if ok:
                            set_exists = True
                            uploaded_names.append(name)
                    else:
                        ok = await add_emoji_to_set(http, bot_token, sname, png, "⚡")
                        if ok:
                            uploaded_names.append(name)

                # Map positions at end
                set_data = await get_sticker_set(http, bot_token, sname)
                if set_data:
                    stickers = set_data.get("stickers", [])
                    all_sorted = sorted(all_sprites.keys())
                    for idx, s in enumerate(stickers):
                        if idx < len(all_sorted):
                            eid = s.get("custom_emoji_id", "")
                            if eid:
                                type_map[all_sorted[idx]] = eid

        map_file.write_text(json.dumps(type_map, indent=2))
        print(f"  💾 Saved {map_file.name} ({len(type_map)} types)")


# ──────────────────────────────────────────────
# Remap: Re-read existing sticker sets and rebuild maps
# ──────────────────────────────────────────────
async def remap_pokemon() -> None:
    """Re-read all Pokemon sticker sets and rebuild per-bot emoji_map_{bot_id}.json.

    Searches for sets under ALL known bot usernames (current bots + legacy)
    and ALL known version prefixes (v2, v3).
    """
    print("\n" + "=" * 60)
    print("  REMAP POKEMON EMOJI")
    print("=" * 60)

    all_usernames = _all_known_usernames()
    print(f"  Searching usernames: {', '.join(all_usernames)}")
    total_sets = (1025 + MAX_PER_SET - 1) // MAX_PER_SET

    for bot in _bots:
        bot_token = bot["token"]
        bot_id = bot["bot_id"]
        username = bot["username"]
        map_file = pokemon_map_path(bot_id)
        emoji_map: dict[str, str] = {}

        # Search this bot's username + legacy usernames
        search_names = [username] + [u for u in _LEGACY_USERNAMES if u != username]

        async with aiohttp.ClientSession() as http:
            for batch in range(1, total_sets + 1):
                batch_start = (batch - 1) * MAX_PER_SET + 1
                batch_end = min(batch * MAX_PER_SET, 1025)
                found = False

                for uname in search_names:
                    snames_to_try = [f"telemon{batch}_by_{uname}", f"telemon_emoji_v4_{batch}_by_{uname}", f"telemon_emoji_v3_{batch}_by_{uname}", f"telemon_emoji_v2_{batch}_by_{uname}"]
                    for sname in snames_to_try:
                        set_data = await get_sticker_set(http, bot_token, sname)
                        if set_data:
                            stickers = set_data.get("stickers", [])
                            mapped = 0
                            for idx, s in enumerate(stickers):
                                dex = batch_start + idx
                                if dex <= batch_end:
                                    eid = s.get("custom_emoji_id", "")
                                    if eid:
                                        emoji_map[str(dex)] = eid
                                        mapped += 1
                            print(
                                f"  Set {batch} ({sname}): "
                                f"{len(stickers)} stickers → {mapped} mapped"
                            )
                            found = True
                            break
                    if found:
                        break

                if not found:
                    print(f"  Set {batch}: NOT FOUND (dex {batch_start}-{batch_end})")

        map_file.write_text(json.dumps(emoji_map, indent=2))
        print(f"\n  💾 @{username}: {map_file.name} ({len(emoji_map)} total)")


async def remap_items() -> None:
    """Re-read item sticker set and rebuild per-bot item_emoji_map_{bot_id}.json."""
    print("\n  Re-mapping items...")
    items = get_bot_items()

    for bot in _bots:
        bot_token = bot["token"]
        bot_id = bot["bot_id"]
        username = bot["username"]
        map_file = item_map_path(bot_id)
        item_map: dict[str, str] = {}

        search_names = [username] + [u for u in _LEGACY_USERNAMES if u != username]

        async with aiohttp.ClientSession() as http:
            for uname in search_names:
                sname = item_set_name(1, uname)
                set_data = await get_sticker_set(http, bot_token, sname)
                if set_data:
                    stickers = set_data.get("stickers", [])
                    sorted_items = sorted(items, key=lambda x: x["slug"])
                    for idx, s in enumerate(stickers):
                        if idx < len(sorted_items):
                            eid = s.get("custom_emoji_id", "")
                            if eid:
                                item_map[sorted_items[idx]["slug"]] = eid
                    print(f"  @{username}: Mapped {len(item_map)} item emoji (from @{uname})")
                    break
            else:
                print(f"  @{username}: Item set not found!")

        map_file.write_text(json.dumps(item_map, indent=2))


async def remap_types() -> None:
    """Re-read type sticker set and rebuild per-bot type_emoji_map_{bot_id}.json."""
    print("\n  Re-mapping types...")
    types = load_types_csv()
    main_types = sorted(
        [(tid, name) for tid, name in types.items() if 1 <= tid <= 18],
        key=lambda x: x[1],
    )

    for bot in _bots:
        bot_token = bot["token"]
        bot_id = bot["bot_id"]
        username = bot["username"]
        map_file = type_map_path(bot_id)
        type_map: dict[str, str] = {}

        search_names = [username] + [u for u in _LEGACY_USERNAMES if u != username]

        async with aiohttp.ClientSession() as http:
            for uname in search_names:
                sname = f"telemon_types_by_{uname}"
                set_data = await get_sticker_set(http, bot_token, sname)
                if set_data:
                    stickers = set_data.get("stickers", [])
                    for idx, s in enumerate(stickers):
                        if idx < len(main_types):
                            eid = s.get("custom_emoji_id", "")
                            if eid:
                                type_map[main_types[idx][1]] = eid
                    print(f"  @{username}: Mapped {len(type_map)} type emoji (from @{uname})")
                    break
            else:
                print(f"  @{username}: Type set not found!")

        map_file.write_text(json.dumps(type_map, indent=2))


# ──────────────────────────────────────────────
# Main entrypoint
# ──────────────────────────────────────────────
async def main() -> None:
    args = sys.argv[1:] if len(sys.argv) > 1 else ["help"]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    if not args or args[0] == "help":
        print(
            "Usage: python -m telemon.scripts.upload_emoji <command> [--dry-run]\n"
            "\n"
            "Commands:\n"
            "  pokemon   Upload Pokemon sprite emoji (1025 species)\n"
            "  items     Upload item emoji (bot shop items)\n"
            "  types     Upload type icon emoji (18 types)\n"
            "  all       Upload everything\n"
            "  remap     Re-read existing sticker sets & rebuild all maps\n"
            "  help      Show this help\n"
            "\n"
            "Options:\n"
            "  --dry-run  Test sprite loading without uploading\n"
        )
        return

    cmd = args[0].lower()
    t0 = time.time()

    # Resolve all bot usernames from Telegram API
    if cmd != "help":
        await _init_all_bots()
        global _api_sem, _throttle_lock
        _api_sem = asyncio.Semaphore(_API_CONCURRENCY)
        _throttle_lock = asyncio.Lock()

    if cmd == "pokemon":
        await upload_pokemon(dry_run)
    elif cmd == "items":
        await upload_items(dry_run)
    elif cmd == "types":
        await upload_types(dry_run)
    elif cmd == "all":
        await upload_pokemon(dry_run)
        await upload_items(dry_run)
        await upload_types(dry_run)
    elif cmd == "remap":
        await remap_pokemon()
        await remap_items()
        await remap_types()
    else:
        print(f"Unknown command: {cmd}")
        return

    total_elapsed = elapsed_str(t0)
    print(f"\n⏱  Total time: {total_elapsed}")


if __name__ == "__main__":
    asyncio.run(main())
