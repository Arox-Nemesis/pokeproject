"""Pokemon spawn image generator.

Downloads official artwork PNGs (transparent background) and composites
them onto type-themed gradient backgrounds for a polished spawn display.
Images are cached locally for fast reuse.
"""

import asyncio
import io
import math
import random
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFilter

from telemon.logging import get_logger

logger = get_logger(__name__)

# Cache directory for generated spawn images
CACHE_DIR = Path(__file__).parent.parent.parent.parent.parent / "data" / "spawn_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Official artwork base URL (475x475, transparent PNG)
ARTWORK_BASE = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork"

# Output image size
IMAGE_SIZE = (512, 512)
POKEMON_SIZE = (380, 380)  # Pokemon artwork size within the image

# Type-based color themes (primary color, secondary color for gradient)
TYPE_COLORS: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "normal":   ((168, 168, 120), (200, 200, 160)),
    "fire":     ((200, 60, 30),   (245, 140, 50)),
    "water":    ((40, 100, 200),  (80, 160, 230)),
    "grass":    ((50, 160, 60),   (120, 200, 80)),
    "electric": ((220, 180, 30),  (250, 220, 80)),
    "ice":      ((100, 190, 220), (170, 225, 245)),
    "fighting": ((160, 50, 40),   (200, 90, 70)),
    "poison":   ((140, 50, 160),  (180, 100, 200)),
    "ground":   ((180, 150, 80),  (220, 190, 120)),
    "flying":   ((130, 140, 220), (170, 180, 245)),
    "psychic":  ((220, 60, 120),  (245, 130, 170)),
    "bug":      ((130, 170, 30),  (170, 200, 70)),
    "rock":     ((160, 140, 90),  (200, 180, 120)),
    "ghost":    ((80, 60, 140),   (120, 90, 180)),
    "dragon":   ((90, 50, 200),   (140, 100, 230)),
    "dark":     ((90, 70, 60),    (130, 100, 90)),
    "steel":    ((150, 150, 170), (190, 190, 210)),
    "fairy":    ((220, 140, 180), (240, 180, 210)),
}

# Default colors if type not found
DEFAULT_COLORS = ((120, 140, 160), (160, 180, 200))


def _create_gradient_background(
    primary_type: str,
    size: tuple[int, int] = IMAGE_SIZE,
) -> Image.Image:
    """Create a gradient background based on Pokemon type."""
    colors = TYPE_COLORS.get(primary_type, DEFAULT_COLORS)
    primary, secondary = colors

    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)

    # Radial-ish gradient: darker at edges, lighter toward center
    cx, cy = size[0] // 2, size[1] // 2
    max_dist = math.sqrt(cx ** 2 + cy ** 2)

    for y in range(size[1]):
        for x in range(size[0]):
            # Distance from center (normalized 0-1)
            dist = math.sqrt((x - cx) ** 2 + (y - cy) ** 2) / max_dist

            # Interpolate from secondary (center/bright) to primary (edges/dark)
            r = int(secondary[0] + (primary[0] - secondary[0]) * dist)
            g = int(secondary[1] + (primary[1] - secondary[1]) * dist)
            b = int(secondary[2] + (primary[2] - secondary[2]) * dist)

            draw.point((x, y), fill=(r, g, b))

    return img


def _create_gradient_fast(
    primary_type: str,
    size: tuple[int, int] = IMAGE_SIZE,
) -> Image.Image:
    """Create a gradient background using fast method (small + upscale + blur)."""
    colors = TYPE_COLORS.get(primary_type, DEFAULT_COLORS)
    primary, secondary = colors

    # Create at 1/8 size then upscale for performance
    small_size = (size[0] // 8, size[1] // 8)
    img = Image.new("RGB", small_size)
    draw = ImageDraw.Draw(img)

    cx, cy = small_size[0] // 2, small_size[1] // 2
    max_dist = math.sqrt(cx ** 2 + cy ** 2)

    for y in range(small_size[1]):
        for x in range(small_size[0]):
            dist = math.sqrt((x - cx) ** 2 + (y - cy) ** 2) / max_dist
            # Add slight variation for texture
            noise = random.uniform(-0.03, 0.03)
            dist = max(0.0, min(1.0, dist + noise))

            r = int(secondary[0] + (primary[0] - secondary[0]) * dist)
            g = int(secondary[1] + (primary[1] - secondary[1]) * dist)
            b = int(secondary[2] + (primary[2] - secondary[2]) * dist)

            draw.point((x, y), fill=(r, g, b))

    # Upscale with smooth interpolation
    img = img.resize(size, Image.LANCZOS)
    # Apply slight blur for smoothness
    img = img.filter(ImageFilter.GaussianBlur(radius=3))

    return img


def _add_subtle_pattern(img: Image.Image) -> Image.Image:
    """Add a subtle circular vignette and soft ground shadow area."""
    draw = ImageDraw.Draw(img)
    w, h = img.size

    # Darken bottom slightly to create a "ground" feel
    for y in range(h * 3 // 4, h):
        progress = (y - h * 3 // 4) / (h // 4)
        alpha = int(40 * progress)
        for x in range(w):
            pixel = img.getpixel((x, y))
            r = max(0, pixel[0] - alpha)
            g = max(0, pixel[1] - alpha)
            b = max(0, pixel[2] - alpha)
            draw.point((x, y), fill=(r, g, b))

    return img


def _composite_pokemon(
    background: Image.Image,
    artwork: Image.Image,
    pokemon_size: tuple[int, int] = POKEMON_SIZE,
) -> Image.Image:
    """Composite Pokemon artwork onto background, centered."""
    # Resize artwork to fit, maintaining aspect ratio
    artwork_copy = artwork.copy()
    artwork_copy.thumbnail(pokemon_size, Image.LANCZOS)

    # Center the Pokemon on the background
    bg_w, bg_h = background.size
    art_w, art_h = artwork_copy.size

    # Position slightly above center for a natural look
    x = (bg_w - art_w) // 2
    y = (bg_h - art_h) // 2 - 10  # Slight upward offset

    # Create a subtle drop shadow
    shadow = Image.new("RGBA", background.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    # Elliptical shadow below the Pokemon
    shadow_cx = bg_w // 2
    shadow_cy = y + art_h + 5
    shadow_rx = art_w // 3
    shadow_ry = 15
    shadow_draw.ellipse(
        [shadow_cx - shadow_rx, shadow_cy - shadow_ry,
         shadow_cx + shadow_rx, shadow_cy + shadow_ry],
        fill=(0, 0, 0, 40),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=8))

    # Composite: background + shadow + pokemon
    result = background.convert("RGBA")
    result = Image.alpha_composite(result, shadow)
    result.paste(artwork_copy, (x, y), artwork_copy)

    return result.convert("RGB")


async def download_artwork(dex_number: int, shiny: bool = False) -> Image.Image | None:
    """Download official artwork PNG for a Pokemon."""
    if shiny:
        url = f"{ARTWORK_BASE}/shiny/{dex_number}.png"
    else:
        url = f"{ARTWORK_BASE}/{dex_number}.png"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return Image.open(io.BytesIO(resp.content))
            else:
                logger.warning("Failed to download artwork", dex=dex_number, status=resp.status_code)
                return None
    except Exception as e:
        logger.error("Error downloading artwork", dex=dex_number, error=str(e))
        return None


async def generate_spawn_image(
    dex_number: int,
    primary_type: str,
    shiny: bool = False,
) -> io.BytesIO | None:
    """Generate a spawn image with Pokemon on typed background.

    Returns a BytesIO object containing a JPEG image, or None on failure.
    Results are cached locally for fast reuse.
    """
    # Check cache first
    cache_key = f"{'shiny_' if shiny else ''}{dex_number}.jpg"
    cache_path = CACHE_DIR / cache_key

    if cache_path.exists():
        buf = io.BytesIO(cache_path.read_bytes())
        buf.seek(0)
        return buf

    # Download artwork
    artwork = await download_artwork(dex_number, shiny=shiny)
    if artwork is None:
        return None

    # Generate in thread pool to not block event loop
    loop = asyncio.get_event_loop()
    result_bytes = await loop.run_in_executor(
        None, _generate_image_sync, artwork, primary_type
    )

    if result_bytes is None:
        return None

    # Cache to disk
    try:
        cache_path.write_bytes(result_bytes)
    except Exception as e:
        logger.warning("Failed to cache image", error=str(e))

    buf = io.BytesIO(result_bytes)
    buf.seek(0)
    return buf


def _generate_image_sync(artwork: Image.Image, primary_type: str) -> bytes | None:
    """Synchronous image generation (runs in thread pool)."""
    try:
        # Create typed gradient background
        background = _create_gradient_fast(primary_type)

        # Add subtle ground pattern
        background = _add_subtle_pattern(background)

        # Composite Pokemon onto background
        result = _composite_pokemon(background, artwork)

        # Save as JPEG for smaller file size
        buf = io.BytesIO()
        result.save(buf, format="JPEG", quality=90, optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.error("Error generating spawn image", error=str(e))
        return None


async def clear_cache() -> int:
    """Clear the spawn image cache. Returns number of files deleted."""
    count = 0
    for f in CACHE_DIR.glob("*.jpg"):
        f.unlink()
        count += 1
    return count
