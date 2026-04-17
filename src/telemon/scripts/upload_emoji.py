"""Upload all Pokemon sprites as Telegram custom emoji and save ID mapping.

Creates custom emoji sticker sets (max 200 per set, so 6 sets for 1025 species).
Downloads sprites from PokeAPI, resizes to 100x100, uploads as custom emoji.
Saves the dex_number -> custom_emoji_id mapping to data/emoji_map.json.

Usage:
    python -m telemon.scripts.upload_emoji

This takes ~20-30 minutes due to Telegram rate limits.
Resumable: skips species already in emoji_map.json.
"""

import asyncio
import io
import json
import sys
import time
from pathlib import Path

import aiohttp
from PIL import Image

# Config
BOT_USERNAME = "TelemonXRobot"
OWNER_ID = 6894738352
TOTAL_SPECIES = 1025
MAX_PER_SET = 200
SPRITE_SIZE = 100  # 100x100 pixels

DATA_DIR = Path(__file__).parent.parent.parent.parent / "data"
EMOJI_MAP_FILE = DATA_DIR / "emoji_map.json"
SPRITE_BASE = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/{dex}.png"

# Load pokemon names for nicer output
POKEMON_NAMES: dict[int, str] = {}
POKEMON_JSON = DATA_DIR / "pokemon.json"
if POKEMON_JSON.exists():
    for entry in json.loads(POKEMON_JSON.read_text()):
        POKEMON_NAMES[entry["national_dex"]] = entry["name"]


def get_bot_token() -> str:
    env_path = Path(__file__).parent.parent.parent.parent / ".env"
    for line in env_path.read_text().splitlines():
        if line.startswith("BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("BOT_TOKEN not found in .env")


def poke_label(dex: int) -> str:
    name = POKEMON_NAMES.get(dex, "???")
    return f"#{dex:04d} {name}"


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


def set_name_for_batch(batch_num: int) -> str:
    return f"telemon_emoji_v2_{batch_num}_by_{BOT_USERNAME}"


def set_title_for_batch(batch_num: int, start: int, end: int) -> str:
    return f"Telemon Emoji v2 #{start:04d}-#{end:04d}"


LOCAL_SPRITE_DIR = DATA_DIR / "sprites" / "pokemon"


async def download_sprite(http: aiohttp.ClientSession, dex: int) -> bytes | None:
    """Download and resize a sprite to 100x100 PNG using sharp pixel-art scaling.

    Checks local sprite directory first, falls back to PokeAPI remote download.
    """
    raw: bytes | None = None

    # Try local sprite first
    local_path = LOCAL_SPRITE_DIR / f"{dex}.png"
    if local_path.exists():
        try:
            raw = local_path.read_bytes()
        except Exception:
            raw = None

    # Fallback to remote download
    if not raw or len(raw) < 100:
        url = SPRITE_BASE.format(dex=dex)
        try:
            async with http.get(url) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.read()
                if len(raw) < 100:
                    return None
        except Exception as e:
            print(f"  [WARN] Failed to download sprite {poke_label(dex)}: {e}")
            return None

    try:
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        
        # 1. Crop the empty transparent space to make the sprite as large as possible
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
            
        # 2. Calculate new size to fit exactly into SPRITE_SIZE x SPRITE_SIZE (100x100)
        ratio = min(SPRITE_SIZE / img.width, SPRITE_SIZE / img.height)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        
        # 3. Resize using NEAREST to maintain sharp, unblurred pixel edges
        img = img.resize(new_size, Image.NEAREST)
        
        # 4. Paste it perfectly in the center of a SPRITE_SIZE x SPRITE_SIZE transparent canvas
        final_img = Image.new("RGBA", (SPRITE_SIZE, SPRITE_SIZE), (0, 0, 0, 0))
        offset = ((SPRITE_SIZE - new_size[0]) // 2, (SPRITE_SIZE - new_size[1]) // 2)
        final_img.paste(img, offset)

        buf = io.BytesIO()
        final_img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        print(f"  [WARN] Failed to process sprite {poke_label(dex)}: {e}")
        return None


async def create_emoji_set(
    http: aiohttp.ClientSession,
    bot_token: str,
    set_name: str,
    title: str,
    png_data: bytes,
) -> bool:
    """Create a new custom emoji sticker set with one initial emoji."""
    form = aiohttp.FormData()
    form.add_field("user_id", str(OWNER_ID))
    form.add_field("name", set_name)
    form.add_field("title", title)
    form.add_field("sticker_type", "custom_emoji")
    form.add_field("stickers", json.dumps([{
        "sticker": "attach://file0",
        "emoji_list": ["🔴"],
        "format": "static",
    }]))
    form.add_field("file0", png_data, filename="sprite.png", content_type="image/png")

    resp = await http.post(
        f"https://api.telegram.org/bot{bot_token}/createNewStickerSet",
        data=form,
    )
    result = await resp.json()
    if not result.get("ok"):
        print(f"  [ERR] createNewStickerSet failed: {result.get('description', result)}")
        return False
    return True


async def add_emoji_to_set(
    http: aiohttp.ClientSession,
    bot_token: str,
    set_name: str,
    png_data: bytes,
) -> bool:
    """Add one emoji to an existing set."""
    form = aiohttp.FormData()
    form.add_field("user_id", str(OWNER_ID))
    form.add_field("name", set_name)
    form.add_field("sticker", json.dumps({
        "sticker": "attach://file0",
        "emoji_list": ["🔴"],
        "format": "static",
    }))
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
            return await add_emoji_to_set(http, bot_token, set_name, png_data)
        print(f"  [ERR] addStickerToSet failed: {desc}")
        return False
    return True


async def get_emoji_ids(
    http: aiohttp.ClientSession,
    bot_token: str,
    set_name: str,
) -> list[str]:
    """Get all custom_emoji_ids from a sticker set, in order."""
    resp = await http.get(
        f"https://api.telegram.org/bot{bot_token}/getStickerSet",
        params={"name": set_name},
    )
    result = await resp.json()
    if not result.get("ok"):
        return []
    return [
        s.get("custom_emoji_id", "")
        for s in result["result"]["stickers"]
    ]


async def main() -> None:
    bot_token = get_bot_token()
    t0 = time.time()

    # Load existing mapping (for resume)
    emoji_map: dict[str, str] = {}
    if EMOJI_MAP_FILE.exists():
        emoji_map = json.loads(EMOJI_MAP_FILE.read_text())

    already_done = {int(k) for k in emoji_map.keys()}
    remaining = [d for d in range(1, TOTAL_SPECIES + 1) if d not in already_done]

    print("=" * 60)
    print("  TELEMON — Custom Emoji Uploader")
    print("=" * 60)
    print(f"  Total species:    {TOTAL_SPECIES}")
    print(f"  Already uploaded: {len(already_done)}")
    print(f"  Remaining:        {len(remaining)}")
    print(f"  Sticker sets:     {(TOTAL_SPECIES + MAX_PER_SET - 1) // MAX_PER_SET} (max {MAX_PER_SET}/set)")
    print("=" * 60)

    if not remaining:
        print("\n✅ All 1025 species already uploaded!")
        return

    # --- Phase 1: Download sprites ---
    print(f"\n📥 PHASE 1: Downloading {len(remaining)} sprites...\n")
    sprites: dict[int, bytes] = {}
    dl_start = time.time()

    async with aiohttp.ClientSession() as http:
        for i, dex in enumerate(remaining):
            data = await download_sprite(http, dex)
            if data:
                sprites[dex] = data
            done = i + 1
            if done % 50 == 0 or done == len(remaining):
                print(f"  {progress_bar(done, len(remaining))}  "
                      f"elapsed {elapsed_str(dl_start)}  "
                      f"ETA {eta_str(dl_start, done, len(remaining))}")

    failed_dl = len(remaining) - len(sprites)
    print(f"\n  ✅ Downloaded: {len(sprites)}  |  ❌ Failed: {failed_dl}")

    if not sprites:
        print("\nNo sprites to upload. Exiting.")
        return

    # --- Phase 2: Upload to Telegram ---
    print(f"\n📤 PHASE 2: Uploading {len(sprites)} emoji to Telegram...\n")

    def batch_for_dex(dex: int) -> int:
        return (dex - 1) // MAX_PER_SET + 1

    batches: dict[int, list[int]] = {}
    for dex in sorted(sprites.keys()):
        b = batch_for_dex(dex)
        batches.setdefault(b, []).append(dex)

    upload_done = 0
    upload_fail = 0
    upload_total = len(sprites)
    upload_start = time.time()

    async with aiohttp.ClientSession() as http:
        for batch_num in sorted(batches.keys()):
            dex_list = batches[batch_num]
            sname = set_name_for_batch(batch_num)
            batch_start = (batch_num - 1) * MAX_PER_SET + 1
            batch_end = min(batch_num * MAX_PER_SET, TOTAL_SPECIES)

            print(f"\n  ── Set {batch_num}/{(TOTAL_SPECIES + MAX_PER_SET - 1) // MAX_PER_SET}"
                  f"  [{sname}]  ({len(dex_list)} emoji) ──")

            # Check if set already exists
            resp = await http.get(
                f"https://api.telegram.org/bot{bot_token}/getStickerSet",
                params={"name": sname},
            )
            set_data = await resp.json()
            set_exists = set_data.get("ok", False)
            existing_count = len(set_data.get("result", {}).get("stickers", [])) if set_exists else 0

            if set_exists:
                print(f"  Set exists with {existing_count} emoji already")

            for i, dex in enumerate(dex_list):
                png = sprites[dex]
                label = poke_label(dex)

                if not set_exists:
                    title = set_title_for_batch(batch_num, batch_start, batch_end)
                    ok = await create_emoji_set(http, bot_token, sname, title, png)
                    if ok:
                        set_exists = True
                        existing_count = 1
                        upload_done += 1
                        print(f"  ✅ Created set + {label}")
                    else:
                        upload_fail += 1
                        print(f"  ❌ FAIL create {label}")
                        continue
                else:
                    ok = await add_emoji_to_set(http, bot_token, sname, png)
                    if ok:
                        existing_count += 1
                        upload_done += 1
                    else:
                        upload_fail += 1

                # Progress line every 5 uploads or on last one
                if upload_done % 5 == 0 or (i + 1) == len(dex_list):
                    print(f"  {progress_bar(upload_done, upload_total)}  "
                          f"{label}  "
                          f"elapsed {elapsed_str(upload_start)}  "
                          f"ETA {eta_str(upload_start, upload_done, upload_total)}")

                await asyncio.sleep(0.35)

            # After batch: fetch emoji IDs and map them
            if set_exists:
                print(f"\n  🔍 Mapping emoji IDs for set {batch_num}...")
                resp2 = await http.get(
                    f"https://api.telegram.org/bot{bot_token}/getStickerSet",
                    params={"name": sname},
                )
                set_info = await resp2.json()
                if set_info.get("ok"):
                    stickers = set_info["result"]["stickers"]
                    mapped = 0
                    for idx, s in enumerate(stickers):
                        d = batch_start + idx
                        if d <= batch_end:
                            eid = s.get("custom_emoji_id", "")
                            if eid:
                                emoji_map[str(d)] = eid
                                mapped += 1
                    print(f"  📎 Mapped {mapped} emoji IDs")

                # Save after each batch (crash-safe resume)
                EMOJI_MAP_FILE.write_text(json.dumps(emoji_map, indent=2))
                print(f"  💾 Saved to emoji_map.json ({len(emoji_map)} total)")

    # --- Summary ---
    total_elapsed = elapsed_str(t0)
    print("\n" + "=" * 60)
    print("  UPLOAD COMPLETE")
    print("=" * 60)
    print(f"  ✅ Uploaded:  {upload_done}")
    print(f"  ❌ Failed:    {upload_fail}")
    print(f"  📎 Mapped:    {len(emoji_map)} emoji IDs")
    print(f"  ⏱  Time:     {total_elapsed}")
    print(f"  📁 Saved to:  {EMOJI_MAP_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
