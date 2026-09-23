"""Fighter headshot graphics for embeds.

ESPN serves every UFC fighter's headshot as a transparent 600x436 PNG. Two of them
are composed side by side on a dark card: corner colours for a preview, and the
winner in colour with the loser greyed out for a result.
"""

from __future__ import annotations

import asyncio
import io
import time
from collections import OrderedDict

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from ..models import Fighter
from ..sources.http import HttpClient
from .common import surname

WIDTH, HEIGHT = 1000, 420
HALF = WIDTH // 2
BACKGROUND = (30, 31, 34)
RED_CORNER = (200, 16, 46)
BLUE_CORNER = (32, 92, 196)
GOLD = (232, 185, 59)
LOSER_TINT = (70, 70, 74)
NAME_STRIP = 64
IMAGE_NAME = "matchup.jpg"

# Headshots are held by size rather than by count, because they vary: measured
# across a twelve-fight card they run 230KB each and a full card comes to 4.7MB.
# The cap is set above that so nothing is evicted part way through a card and
# fetched again for the result post.
CACHE_BYTES = 6 * 1024 * 1024
MAX_HEADSHOT_BYTES = 3_000_000

# ...but a card is four hours and the gap to the next one is a fortnight, and
# these are only ever wanted while a card is on. Holding 4.7MB of faces for two
# weeks to save re-fetching them is the wrong trade, so anything untouched for
# this long is dropped. During a card every headshot is read repeatedly, for the
# walkout and again for the result, so nothing in use ever ages out.
HEADSHOT_TTL = 6 * 60 * 60


class MatchupImages:
    def __init__(
        self, http: HttpClient, *, cache_bytes: int = CACHE_BYTES, ttl: float = HEADSHOT_TTL
    ) -> None:
        self.http = http
        self._cache_bytes = cache_bytes
        self._ttl = ttl
        self._held = 0
        # url -> (last read, image), oldest read first.
        self._cache: OrderedDict[str, tuple[float, bytes | None]] = OrderedDict()

    async def headshot(self, url: str | None) -> bytes | None:
        if not url:
            return None
        now = time.monotonic()
        cached = self._cache.get(url)
        if cached is not None:
            self._cache[url] = (now, cached[1])
            self._cache.move_to_end(url)
            self._expire(now)
            return cached[1]

        data = await self.http.get_bytes(url, max_bytes=MAX_HEADSHOT_BYTES)
        if data is not None and not data.startswith(b"\x89PNG"):
            data = None
        self._cache[url] = (now, data)
        self._cache.move_to_end(url)
        self._held += len(data) if data else 0
        self._expire(now)
        return data

    def _expire(self, now: float) -> None:
        """Drop what has gone cold, then what is oldest, until within the cap.

        Both run oldest-first, and the cache is ordered by when each image was
        last read, so a card in progress keeps everything it is using.
        """
        while self._cache:
            url, (read_at, data) = next(iter(self._cache.items()))
            if now - read_at < self._ttl and self._held <= self._cache_bytes:
                break
            self._cache.pop(url)
            self._held -= len(data) if data else 0

    async def matchup(self, a: Fighter, b: Fighter, *, winner_id: str | None = None) -> bytes | None:
        """JPEG of both fighters, or None when neither has a headshot."""
        left, right = await asyncio.gather(self.headshot(a.headshot_url), self.headshot(b.headshot_url))
        if left is None and right is None:
            return None
        winner = None
        if winner_id is not None:
            winner = "a" if winner_id == a.id else "b" if winner_id == b.id else None
        return await asyncio.to_thread(render_matchup, left, right, a.display_name, b.display_name, winner=winner)


def _font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _tint(canvas: Image.Image, x0: int, colour: tuple[int, int, int], *, strong_on_left: bool, strength: float) -> None:
    """A colour wash that is strongest at the outer edge and fades toward the centre."""
    band = Image.new("RGB", (HALF, HEIGHT), colour)
    mask = Image.linear_gradient("L").rotate(-90 if strong_on_left else 90).resize((HALF, HEIGHT))
    mask = mask.point(lambda v: int(v * strength))
    canvas.paste(band, (x0, 0), mask)


def _silhouette(width: int, height: int) -> Image.Image:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    grey = (96, 98, 104, 255)
    draw.ellipse((width * 0.36, height * 0.10, width * 0.64, height * 0.50), fill=grey)
    draw.rounded_rectangle((width * 0.20, height * 0.55, width * 0.80, height * 1.2), radius=int(width * 0.18), fill=grey)
    return image


def _prepare(png: bytes | None, *, greyed: bool) -> Image.Image:
    target_height = HEIGHT - 16
    if png is None:
        portrait = _silhouette(HALF, target_height)
    else:
        portrait = Image.open(io.BytesIO(png)).convert("RGBA")
        scale = target_height / portrait.height
        portrait = portrait.resize((int(portrait.width * scale), target_height), Image.LANCZOS)
        if portrait.width > HALF:
            # Headshots are centred, so trimming the sides keeps the face.
            left = (portrait.width - HALF) // 2
            portrait = portrait.crop((left, 0, left + HALF, target_height))
    if greyed:
        alpha = portrait.getchannel("A")
        grey = ImageEnhance.Brightness(ImageOps.grayscale(portrait.convert("RGB"))).enhance(0.5).convert("RGBA")
        grey.putalpha(alpha)
        portrait = grey
    return portrait


def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, size: int) -> ImageFont.ImageFont:
    while size > 16:
        font = _font(size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 2
    return _font(size)


def render_matchup(
    left: bytes | None,
    right: bytes | None,
    left_name: str,
    right_name: str,
    *,
    winner: str | None = None,
) -> bytes:
    """Compose the card. ``winner`` is "a", "b" or None for a preview."""
    canvas = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)

    if winner is None:
        _tint(canvas, 0, RED_CORNER, strong_on_left=True, strength=0.55)
        _tint(canvas, HALF, BLUE_CORNER, strong_on_left=False, strength=0.55)
    else:
        _tint(canvas, 0, GOLD if winner == "a" else LOSER_TINT, strong_on_left=True, strength=0.5)
        _tint(canvas, HALF, GOLD if winner == "b" else LOSER_TINT, strong_on_left=False, strength=0.5)

    for png, x0, side in ((left, 0, "a"), (right, HALF, "b")):
        portrait = _prepare(png, greyed=winner is not None and winner != side)
        x = x0 + (HALF - portrait.width) // 2
        canvas.paste(portrait, (x, HEIGHT - portrait.height), portrait)

    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rectangle((0, HEIGHT - NAME_STRIP, WIDTH, HEIGHT), fill=(0, 0, 0, 170))
    for name, centre, side in ((left_name, HALF // 2, "a"), (right_name, HALF + HALF // 2, "b")):
        label = surname(name).upper()
        font = _fit_text(draw, label, HALF - 40, 38)
        colour = GOLD if winner == side else (235, 235, 235) if winner is None else (150, 150, 155)
        draw.text((centre, HEIGHT - NAME_STRIP // 2), label, font=font, fill=colour, anchor="mm")

    if winner is None:
        cx, cy, radius = HALF, (HEIGHT - NAME_STRIP) // 2, 46
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(18, 18, 20, 235), outline=(235, 235, 235), width=3)
        draw.text((cx, cy), "VS", font=_font(38), fill=(235, 235, 235), anchor="mm")
    else:
        centre = HALF // 2 if winner == "a" else HALF + HALF // 2
        font = _font(26)
        width = int(draw.textlength("WINNER", font=font)) + 36
        draw.rounded_rectangle((centre - width // 2, 18, centre + width // 2, 58), radius=20, fill=GOLD)
        draw.text((centre, 38), "WINNER", font=font, fill=(24, 24, 26), anchor="mm")

    draw.line((HALF, 0, HALF, HEIGHT - NAME_STRIP), fill=(0, 0, 0, 90), width=2)

    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=88, optimize=True)
    return out.getvalue()
